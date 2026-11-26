import asyncio
import html
import logging
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import ClientSession, web

from . import core
from .store import base_meta

log = logging.getLogger("web")
STATIC = Path(__file__).resolve().parent.parent / "static"
CHUNK = 4 * 1024 * 1024
IDLE_LIMIT = 3600


class Session:
    def __init__(self, uid, name, size):
        self.uid, self.name, self.size = uid, name, size
        self.key = core.new_key()
        self.lock = asyncio.Lock()
        self.started = self.touched = time.time()
        self.progress_msg = None
        self.last_notify = 0
        self.uploader = None
        self.cancelled = False
        self.complete = False


class Web:
    def __init__(self, store, bot, cfg, client):
        self.store, self.bot, self.cfg, self.client = store, bot, cfg, client
        self.sessions = {}
        self.downloads = 0
        self.started = time.time()
        self.bytes_in = 0
        self.bytes_out = 0           # estimated outbound since this process started

    def app(self):
        app = web.Application(client_max_size=CHUNK + 64 * 1024, middlewares=[self.headers])
        app.add_routes([
            web.get("/", self.page("dashboard.html")),
            web.get("/upload", self.page("upload.html")),
            web.get("/style.css", self.page("style.css")),
            web.get("/health", self.health),
            web.get("/api/status", self.status),
            web.post("/api/upload", self.create),
            web.get("/api/upload/{sid}", self.query),
            web.put("/api/upload/{sid}", self.chunk),
            web.delete("/api/upload/{sid}", self.cancel),
            web.get("/d/{key}", self.download_page),
            web.get("/dl/{key}", self.download),
        ])
        return app

    @web.middleware
    async def headers(self, request, handler):
        response = await handler(request)
        if not response.prepared:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def page(self, name):
        async def handler(_):
            return web.FileResponse(STATIC / name)
        return handler

    async def health(self, _):
        return web.json_response({"ok": True})

    async def status(self, _):
        count, size = self.store.index.totals()
        return web.json_response({
            "online": self.client.is_connected() and self.store.ready,
            "uptime": int(time.time() - self.started),
            "files": count, "stored": size,
            "uploads": sum(1 for s in self.sessions.values() if not s.complete),
            "downloads": self.downloads,
            "in": self.bytes_in, "out": self.bytes_out,
            "limit": int(self.cfg.bandwidth_limit * 1024 ** 3),
            "active": [{"percent": round(s.uploader.sent * 100 / s.size, 1),
                        "speed": int(s.uploader.sent / max(time.time() - s.started, 1)),
                        "size": s.size}
                       for s in self.sessions.values() if not s.complete],
        })

    # ----- upload API -----
    def auth(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        data = core.unsign(self.cfg.secret, token)
        if not data or "up" not in data or not self.cfg.may_upload(data["up"]):
            raise web.HTTPUnauthorized(text="Link expired. Send /upload to the bot again.")
        if not self.store.ready:
            raise web.HTTPServiceUnavailable(text="Bot is starting, retry shortly.")
        return data["up"]

    def session(self, request):
        uid = self.auth(request)
        s = self.sessions.get(request.match_info["sid"])
        if not s or s.uid != uid:
            raise web.HTTPNotFound(text="Upload session not found (server may have restarted).")
        return s

    def state(self, s):
        out = {"received": s.uploader.received, "sent": s.uploader.sent,
               "size": s.size, "done": s.complete}
        if s.complete:
            rec = self.store.index.get(s.key)
            if rec:
                out.update(key=rec.key, link=self.bot.web_link(rec, s.uid),
                           bot=f"https://t.me/{self.bot.username}?start={rec.key}")
        return out

    async def create(self, request):
        uid = self.auth(request)
        try:
            body = await request.json()
            name, size = core.clean_name(str(body["name"])), int(body["size"])
        except Exception:
            raise web.HTTPBadRequest(text="Bad request")
        if size <= 0 or size > self.cfg.max_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=self.cfg.max_bytes, actual_size=size,
                text=f"File must be between 1 byte and {core.human(self.cfg.max_bytes)}")
        if sum(1 for s in self.sessions.values() if not s.complete) >= self.cfg.max_uploads:
            raise web.HTTPTooManyRequests(text="Server busy. Try again soon.")
        s = Session(uid, name, size)

        async def finish(vol, file_id, total, vsize, big):
            count = s.uploader.count
            meta = base_meta(uid, self.bot.names.get(uid), name, vsize, s.key, vol, count)
            if count > 1:
                meta["p"] = f"{name}.{vol + 1:03d}"
            msg_id = await self.store.post_volume(meta, file_id, total, vsize, big)
            self.store.index.add(msg_id, meta)
            return msg_id

        s.uploader = core.Uploader(size, self.store.save_part, finish)
        sid = core.new_key()
        self.sessions[sid] = s
        return web.json_response({"id": sid, "chunk": CHUNK})

    async def query(self, request):
        return web.json_response(self.state(self.session(request)))

    async def chunk(self, request):
        s = self.session(request)
        data = await request.read()
        async with s.lock:
            if s.cancelled:
                raise web.HTTPNotFound(text="Upload cancelled")
            s.touched = time.time()
            try:
                offset = int(request.query["offset"])
            except (KeyError, ValueError):
                raise web.HTTPBadRequest(text="offset required")
            if offset != s.uploader.received:
                return web.json_response(self.state(s), status=409)
            before = s.uploader.sent
            try:
                if data:
                    await s.uploader.feed(data)
                else:
                    await s.uploader.flush()       # retry after an earlier failure
            except ValueError as e:
                raise web.HTTPBadRequest(text=str(e))
            except Exception:
                log.exception("upload to Telegram failed")
                raise web.HTTPBadGateway(text="Telegram upload failed, retrying")
            finally:
                moved = s.uploader.sent - before
                self.bytes_in += len(data)
                self.bytes_out += moved
                s.touched = time.time()
            s.complete = s.uploader.done
            await self.report(s)
        return web.json_response(self.state(s))

    async def report(self, s):
        if not s.complete and time.time() - s.last_notify < 4:
            return
        s.last_notify = time.time()
        text = core.progress_text(s.name, s.uploader.sent, s.size, s.started)
        s.progress_msg = await self.bot.notify(s.uid, text, s.progress_msg)
        if s.complete:
            rec = self.store.index.get(s.key)
            if rec:
                try:
                    await self.bot.show(s.uid, rec, s.uid)
                except Exception:
                    log.warning("could not send key to user")

    async def discard(self, s):
        s.cancelled = True
        if not s.complete:
            rec = self.store.index.records.get(s.key)
            if rec:
                await self.store.delete(rec)

    async def cancel(self, request):
        s = self.session(request)
        async with s.lock:
            await self.discard(s)
        self.sessions.pop(request.match_info["sid"], None)
        return web.json_response({"ok": True})

    async def cleanup(self):
        while True:
            await asyncio.sleep(300)
            for sid, s in list(self.sessions.items()):
                if time.time() - s.touched > IDLE_LIMIT and not s.lock.locked():
                    try:
                        await self.discard(s)
                    except Exception:
                        log.exception("cleanup failed")
                    self.sessions.pop(sid, None)

    async def keep_alive(self):
        async with ClientSession() as http:
            while True:
                await asyncio.sleep(600)
                try:
                    async with http.get(self.cfg.base_url + "/health"):
                        pass
                except Exception as e:
                    log.warning("keep-alive failed: %s", e)

    # ----- downloads -----
    def record(self, request):
        if not self.store.ready:
            raise web.HTTPServiceUnavailable(text="Bot is starting, retry shortly.")
        key = request.match_info["key"]
        rec = self.store.index.get(key)
        if not rec:
            raise web.HTTPNotFound(text="File not found")
        if rec.allowed:
            t = core.unsign(self.cfg.secret, request.query.get("t", ""))
            uid = t.get("u") if t and t.get("d") == key else None
            if uid is None or not (rec.can_access(uid) or uid in self.cfg.admins):
                raise web.HTTPForbidden(text="Private file. Request a fresh link with /link in the bot.")
        return rec

    async def download_page(self, request):
        rec = self.record(request)
        t = request.query.get("t")
        suffix = f"t={quote(t)}" if t else ""
        if rec.combined:
            buttons = f'<a class="btn" href="/dl/{rec.key}?{suffix}">⬇ Download</a>'
        else:
            buttons = "".join(
                f'<a class="btn" href="/dl/{rec.key}?part={i}&{suffix}">⬇ '
                f'{html.escape(p[2])} · {core.human(p[1])}</a>'
                for i, p in enumerate(rec.ordered))
        page = (STATIC / "download.html").read_text(encoding="utf-8")
        values = {"BUTTONS": buttons, "SIZE": core.human(rec.size), "PARTS": str(rec.count),
                  "NAME": html.escape(rec.name)}      # user text substituted last
        for k, v in values.items():
            page = page.replace("{{" + k + "}}", v)
        return web.Response(text=page, content_type="text/html")

    async def download(self, request):
        rec = self.record(request)
        name = rec.name
        if "part" in request.query or not rec.combined:
            try:
                i = int(request.query["part"])
                if i < 0:
                    raise ValueError
                part = rec.ordered[i]
            except (KeyError, ValueError, IndexError):
                raise web.HTTPBadRequest(text="Invalid part")
            single = core.Record(dict(rec.meta(i), i=0, n=1))
            single.parts = {0: part}
            rec, name = single, part[2]
        size = rec.size
        if size <= 0:
            raise web.HTTPNotFound(text="Empty file")
        try:
            start, end, partial = core.parse_range(request.headers.get("Range"), size)
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        response = web.StreamResponse(status=206 if partial else 200, headers=headers)
        if request.method == "HEAD":
            await response.prepare(request)
            return response
        if self.downloads >= self.cfg.max_downloads:
            raise web.HTTPTooManyRequests(text="Server busy. Try again soon.")
        self.downloads += 1
        try:
            await response.prepare(request)
            async for block in self.store.stream(rec, start, end):
                await response.write(block)
                self.bytes_out += len(block)
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception:
            log.exception("download failed")
            if request.transport:
                request.transport.close()      # signal an incomplete file
        finally:
            self.downloads -= 1
        return response
