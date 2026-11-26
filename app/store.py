"""The private Telegram channel is the database: metadata lives in captions."""
import asyncio
import logging
import time

from telethon import errors, functions, types

from . import core

log = logging.getLogger("store")


class Store:
    def __init__(self, client, cfg):
        self.client, self.cfg = client, cfg
        self.index = core.Index()
        self.channel = None
        self.ready = False
        self.lock = asyncio.Lock()      # serialises caption edits

    async def start(self):
        while self.channel is None:
            try:
                self.channel = await self.client.get_input_entity(self.cfg.channel_id)
            except ValueError:
                log.error("Cannot open the storage channel. Check CHANNEL_ID, make the bot "
                          "an admin, then post a message in the channel. Retrying…")
                await asyncio.sleep(15)
        await self.rebuild()
        self.ready = True

    async def rebuild(self):
        # Bots cannot read history, but can fetch messages by id. Post a probe
        # to learn the newest id, then walk every id in batches.
        probe = await self.client.send_message(self.channel, "🔄 indexing…", silent=True)
        top = probe.id
        await probe.delete()
        index = core.Index()
        for start in range(1, top, 100):
            ids = list(range(start, min(start + 100, top)))
            for msg in await self.client.get_messages(self.channel, ids=ids):
                if msg is None or not msg.media:
                    continue
                meta = core.parse_caption(msg.raw_text)
                if meta:
                    index.add(msg.id, meta)
        self.index = index
        log.info("index rebuilt: %s files", index.totals()[0])

    async def save_reference(self, media, meta):
        """Copy existing Telegram media into the channel (no bytes transferred)."""
        msg = await self.client.send_file(
            self.channel, media, caption=core.make_caption(meta), parse_mode=None)
        self.index.add(msg.id, meta)
        return msg

    # ----- web upload callbacks -----
    async def save_part(self, file_id, index, total, data, big):
        if big:
            request = functions.upload.SaveBigFilePartRequest(file_id, index, total, data)
        else:
            request = functions.upload.SaveFilePartRequest(file_id, index, data)
        for attempt in range(5):
            try:
                if not await self.client(request):
                    raise IOError("Telegram rejected a file part")
                return
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except (IOError, ConnectionError, asyncio.TimeoutError, errors.ServerError):
                await asyncio.sleep(2 ** attempt)
        raise IOError("could not upload a part to Telegram")

    async def post_volume(self, meta, file_id, total, size, big):
        name = meta.get("p", meta["f"])
        if big:
            handle = types.InputFileBig(file_id, total, name)
        else:
            handle = types.InputFile(file_id, total, name, md5_checksum="")
        msg = await self.client.send_file(
            self.channel, handle, caption=core.make_caption(meta), parse_mode=None,
            force_document=True, attributes=[types.DocumentAttributeFilename(name)])
        return msg.id

    async def messages(self, rec):
        msgs = await self.client.get_messages(self.channel, ids=[p[0] for p in rec.ordered])
        if any(m is None or not m.media for m in msgs):
            raise FileNotFoundError("a part was removed from the storage channel")
        return msgs

    async def update(self, rec, **changes):
        """Change name / allowed list / key and persist it into every caption."""
        async with self.lock:
            old = {k: getattr(rec, k) for k in changes}
            old_key = rec.key
            for k, v in changes.items():
                setattr(rec, k, v)
            try:
                captions = [(rec.parts[i][0], core.make_caption(rec.meta(i)))
                            for i in range(rec.count)]
                for msg_id, caption in captions:
                    try:
                        await self.client.edit_message(self.channel, msg_id, caption, parse_mode=None)
                    except errors.MessageNotModifiedError:
                        pass
            except Exception:
                for k, v in old.items():
                    setattr(rec, k, v)
                raise
            if rec.key != old_key:
                self.index.records.pop(old_key, None)
                self.index.records[rec.key] = rec

    async def delete(self, rec):
        await self.client.delete_messages(self.channel, [p[0] for p in rec.parts.values()])
        self.index.remove(rec.key)

    async def stream(self, rec, start, end):
        """Yield bytes [start, end] across all volumes as one continuous file."""
        sizes = [p[1] for p in rec.ordered]
        for vol, offset, length in core.plan(sizes, start, end):
            remaining, pos, failures = length, offset, 0
            while remaining > 0:
                # Refetch for a fresh file reference (they expire).
                msg = await self.client.get_messages(self.channel, ids=rec.ordered[vol][0])
                if msg is None or not msg.media:
                    raise FileNotFoundError("part missing from channel")
                aligned = pos - pos % core.PART
                skip = pos - aligned
                try:
                    async for block in self.client.iter_download(
                            msg.media, offset=aligned, request_size=core.PART,
                            file_size=sizes[vol]):
                        data = bytes(block[skip:skip + remaining])
                        skip = 0
                        if not data:
                            break
                        yield data
                        pos += len(data)
                        remaining -= len(data)
                        failures = 0
                        if remaining <= 0:
                            break
                    if remaining > 0:
                        raise IOError("download ended early")
                except (errors.FileReferenceExpiredError, ConnectionError, IOError,
                        asyncio.TimeoutError) as e:
                    failures += 1
                    if failures > 4:
                        raise
                    log.warning("download retry: %s", type(e).__name__)
                    await asyncio.sleep(failures)


def base_meta(uid, owner_name, name, size, key=None, index=0, count=1, combined=True):
    return {"k": key or core.new_key(), "i": index, "n": count, "s": size,
            "f": core.clean_name(name), "u": uid, "o": (owner_name or "")[:40],
            "t": int(time.time()), "c": int(combined)}
