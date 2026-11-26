"""Pure logic with no third-party imports, so it can be unit tested."""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time

TAG = "#TGF1 "
PART = 512 * 1024                 # Telegram upload/download block
VOLUME = 3900 * PART              # ~1.9 GiB, safely under the 2000 MiB bot limit
BIG_FILE = 10 * 1024 * 1024       # above this Telegram needs the "big file" API
MAX_ALLOWED = 20


def new_key():
    return secrets.token_urlsafe(12)


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def duration(sec):
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def progress_text(name, done, total, started, now=None):
    now = time.time() if now is None else now
    frac = min(done / total, 1) if total else 0
    speed = done / max(now - started, 0.001)
    eta = (total - done) / speed if speed else 0
    bar = "█" * int(frac * 16) + "░" * (16 - int(frac * 16))
    return (f"⚡ {name}\n{bar} {frac * 100:.1f}%\n"
            f"📦 {human(done)} / {human(total)}\n"
            f"🚀 {human(speed)}/s  ⏳ ETA {duration(eta)}")


def clean_name(name):
    name = re.sub(r"[\x00-\x1f/\\]+", "_", name or "").strip(" .")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:150 - len(ext) - 1] + "." + ext) if dot and len(ext) < 20 else name[:150]
    return name or "file"


# ---------- signed tokens ----------
def sign(secret, data, ttl):
    body = dict(data, exp=int(time.time()) + ttl)
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    mac = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{mac}"


def unsign(secret, token):
    try:
        raw, mac = token.split(".")
        good = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(good, mac):
            return None
        body = json.loads(base64.urlsafe_b64decode(raw.encode()))
        return body if body["exp"] >= time.time() else None
    except Exception:
        return None


# ---------- metadata stored in channel captions ----------
def make_caption(meta):
    text = TAG + json.dumps(meta, separators=(",", ":"), ensure_ascii=False)
    head = f"📁 {meta['f']}\n"
    full = head + text
    # Telegram counts caption length in UTF-16 units (limit 1024).
    if len(full.encode("utf-16-le")) // 2 > 1024:
        full = text
    if len(full.encode("utf-16-le")) // 2 > 1024:
        full = TAG + json.dumps(dict(meta, o=""), separators=(",", ":"), ensure_ascii=False)
    if len(full.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("metadata too long: shorten the name or the allowed list")
    return full


def parse_caption(text):
    for line in reversed((text or "").split("\n")):
        if line.startswith(TAG):
            try:
                m = json.loads(line[len(TAG):])
                if all(k in m for k in ("k", "i", "n", "s", "f", "u")):
                    return m
            except ValueError:
                pass
            return None
    return None


class Record:
    def __init__(self, meta):
        self.key = meta["k"]
        self.count = int(meta["n"])
        self.name = meta["f"]
        self.owner = int(meta["u"])
        self.owner_name = meta.get("o", "")
        self.created = meta.get("t", 0)
        self.allowed = list(meta.get("a", []))
        self.combined = bool(meta.get("c", 1))
        self.parts = {}            # index -> (message_id, size, part_name)

    @property
    def complete(self):
        return len(self.parts) == self.count and all(i in self.parts for i in range(self.count))

    @property
    def size(self):
        return sum(p[1] for p in self.parts.values())

    @property
    def ordered(self):
        return [self.parts[i] for i in range(self.count)]

    def can_access(self, uid):
        return not self.allowed or uid == self.owner or uid in self.allowed

    def meta(self, index):
        _, size, part_name = self.parts[index]
        m = {"k": self.key, "i": index, "n": self.count, "s": size, "f": self.name,
             "u": self.owner, "o": self.owner_name[:40], "t": self.created,
             "c": int(self.combined)}
        if self.allowed:
            m["a"] = self.allowed
        if part_name != self.name:
            m["p"] = part_name
        return m


class Index:
    def __init__(self):
        self.records = {}

    def add(self, msg_id, meta):
        rec = self.records.get(meta["k"])
        if rec is None:
            rec = self.records[meta["k"]] = Record(meta)
        elif rec.owner != int(meta["u"]):
            return None
        rec.parts[int(meta["i"])] = (msg_id, int(meta["s"]), meta.get("p", meta["f"]))
        return rec

    def get(self, key):
        rec = self.records.get(key)
        return rec if rec and rec.complete else None

    def owned_by(self, uid):
        return sorted((r for r in self.records.values() if r.owner == uid and r.complete),
                      key=lambda r: r.created, reverse=True)

    def remove(self, key):
        return self.records.pop(key, None)

    def remove_message(self, msg_id):
        for key, rec in list(self.records.items()):
            for i, p in list(rec.parts.items()):
                if p[0] == msg_id:
                    del rec.parts[i]
            if not rec.parts:
                del self.records[key]

    def totals(self):
        done = [r for r in self.records.values() if r.complete]
        return len(done), sum(r.size for r in done)


# ---------- split detection ----------
SPLIT_RE = re.compile(r"^(.+?)\.(?:part)?(\d{2,4})$", re.I)


def detect_split(names):
    """If names are raw split pieces of one file, return (joined_name, order), else None."""
    found = [SPLIT_RE.match(n) for n in names]
    if len(names) < 2 or not all(found):
        return None
    if len({m.group(1) for m in found}) != 1:
        return None
    nums = [int(m.group(2)) for m in found]
    order = sorted(range(len(names)), key=nums.__getitem__)
    first = nums[order[0]]
    if first not in (0, 1) or [nums[i] for i in order] != list(range(first, first + len(names))):
        return None
    return found[0].group(1), order


# ---------- ranged download planning ----------
def parse_range(header, size):
    """Return (start, end_inclusive, is_partial); raise ValueError if unsatisfiable."""
    if not header:
        return 0, size - 1, False
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return 0, size - 1, False
    if not m.group(1):
        length = int(m.group(2))
        if length == 0:
            raise ValueError("empty range")
        return max(size - length, 0), size - 1, True
    start = int(m.group(1))
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, end, True


def plan(sizes, start, end):
    """Map a global byte range to [(volume_index, local_offset, length)]."""
    out, base = [], 0
    for i, size in enumerate(sizes):
        lo, hi = max(start, base), min(end, base + size - 1)
        if lo <= hi:
            out.append((i, lo - base, hi - lo + 1))
        base += size
    return out


# ---------- streaming upload ----------
class Uploader:
    """Turns sequential byte chunks into Telegram volumes without touching disk.

    save_part(file_id, index, total_parts, data, big) uploads one block.
    finish_volume(vol_index, file_id, total_parts, size, big) posts it and
    returns the channel message id.
    """

    def __init__(self, size, save_part, finish_volume, volume=VOLUME, part=PART,
                 parallel=4, big=BIG_FILE):
        if size <= 0:
            raise ValueError("empty file")
        self.size, self.volume, self.part, self.parallel, self.big = size, volume, part, parallel, big
        self.save_part, self.finish_volume = save_part, finish_volume
        self.count = (size + volume - 1) // volume
        self.buf = bytearray()
        self.received = 0          # bytes accepted from the client
        self.sent = 0              # bytes confirmed by Telegram
        self.vol = 0
        self.vpart = 0
        self.file_id = self._new_id()
        self.messages = []         # (message_id, size)

    @staticmethod
    def _new_id():
        return secrets.randbits(62) + 1

    @property
    def done(self):
        return len(self.messages) == self.count

    def _vsize(self):
        return min(self.volume, self.size - self.vol * self.volume)

    async def feed(self, data):
        if self.received + len(data) > self.size:
            raise ValueError("more data than declared size")
        self.buf += data
        self.received += len(data)
        await self.flush()

    async def flush(self):
        # On failure nothing is dropped from the buffer, so calling again resumes.
        while not self.done:
            vsize = self._vsize()
            total = (vsize + self.part - 1) // self.part
            if self.vpart == total:
                big = vsize > self.big
                mid = await self.finish_volume(self.vol, self.file_id, total, vsize, big)
                self.messages.append((mid, vsize))
                self.vol, self.vpart, self.file_id = self.vol + 1, 0, self._new_id()
                continue
            batch, used = [], 0
            while len(batch) < self.parallel and self.vpart + len(batch) < total:
                idx = self.vpart + len(batch)
                length = min(self.part, vsize - idx * self.part)
                if len(self.buf) - used < length:
                    break
                batch.append((idx, bytes(self.buf[used:used + length])))
                used += length
            if not batch:
                return
            results = await asyncio.gather(
                *(self.save_part(self.file_id, i, total, d, vsize > self.big) for i, d in batch),
                return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    raise r
            del self.buf[:used]
            self.vpart += len(batch)
            self.sent += used
