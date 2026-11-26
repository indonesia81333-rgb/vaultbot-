import asyncio
import html
import logging
import time

from telethon import Button, events

from . import core
from .store import base_meta

log = logging.getLogger("bot")

WELCOME = """✨ <b>Cloud File Vault</b> ✨
━━━━━━━━━━━━━━━━
⚡ Send any file → get a <b>secret key</b> instantly
🔑 Anyone with the key gets the file
🌐 Large files (up to {max}) → /upload

<b>Commands</b>
/upload – web upload with live progress
/multi – group split parts under one key, then /done
/files – your files
/get KEY – fetch a file
/info KEY – details
/link KEY – web download link
/rename KEY NEWNAME
/allow KEY USERID – make private and add a user
/deny KEY USERID – remove a user
/public KEY – open to anyone with the key
/newkey KEY – revoke old key
/delete KEY
/id – your user id"""


class Bot:
    def __init__(self, client, store, cfg):
        self.client, self.store, self.cfg = client, store, cfg
        self.username = ""
        self.names = {}              # uid -> display name
        self.batches = {}            # uid -> list of (media, name, size)
        self.albums = asyncio.Lock()
        self.started = time.time()

    async def start(self):
        self.client.parse_mode = "html"
        self.username = (await self.client.get_me()).username
        self.client.add_event_handler(self.on_private, events.NewMessage(
            incoming=True, func=lambda e: e.is_private))
        self.client.add_event_handler(self.on_channel, events.NewMessage(
            chats=self.cfg.channel_id))
        self.client.add_event_handler(self.on_deleted, events.MessageDeleted(
            chats=self.cfg.channel_id))
        self.client.add_event_handler(self.on_button, events.CallbackQuery())

    # ----- helpers -----
    def web_link(self, rec, uid):
        link = f"{self.cfg.base_url}/d/{rec.key}"
        if rec.allowed:
            link += "?t=" + core.sign(self.cfg.secret, {"d": rec.key, "u": uid}, 6 * 3600)
        return link

    def card(self, rec, uid):
        privacy = f"🔒 private ({len(rec.allowed)} users)" if rec.allowed else "🌍 anyone with key"
        text = (f"✅ <b>File ready</b>\n━━━━━━━━━━━━━━━━\n"
                f"📁 <code>{html.escape(rec.name)}</code>\n📦 {core.human(rec.size)}"
                f"  •  🧩 {rec.count} part(s)\n👤 <code>{rec.owner}</code>  •  {privacy}\n\n"
                f"🔑 Key (tap to copy):\n<code>{rec.key}</code>\n\n"
                f"🤖 https://t.me/{self.username}?start={rec.key}\n"
                f"🌐 {self.web_link(rec, uid)}")
        buttons = [[Button.inline("📥 Get file", f"get:{rec.key}".encode())]]
        if uid == rec.owner:
            buttons[0].append(Button.inline("🗑 Delete", f"ask:{rec.key}".encode()))
        return text, buttons

    async def show(self, chat, rec, uid):
        text, buttons = self.card(rec, uid)
        await self.client.send_message(chat, text, buttons=buttons, link_preview=False)

    async def deliver(self, chat, uid, key):
        rec = self.store.index.get(key)
        if not rec or not rec.can_access(uid):
            return await self.client.send_message(chat, "❌ Invalid key or no access.")
        try:
            msgs = await self.store.messages(rec)
        except FileNotFoundError:
            return await self.client.send_message(chat, "❌ This file is no longer in storage.")
        for i, msg in enumerate(msgs, 1):
            caption = f"📁 {rec.name}" + (f"  •  part {i}/{rec.count}" if rec.count > 1 else "")
            await self.client.send_file(chat, msg.media, caption=caption, parse_mode=None)
        if rec.count > 1 and rec.combined:
            await self.client.send_message(
                chat, f"🧩 Download as <b>one combined file</b>:\n{self.web_link(rec, uid)}",
                link_preview=False)

    # ----- incoming messages -----
    async def on_private(self, event):
        if not self.store.ready:
            return await event.reply("⏳ Starting up, try again in a minute.")
        uid = event.sender_id
        sender = await event.get_sender()
        self.names[uid] = " ".join(filter(None, [getattr(sender, "first_name", ""),
                                                 getattr(sender, "last_name", "")]))
        try:
            if event.file and not event.web_preview:
                await self.on_file(event, uid)
            else:
                await self.on_command(event, uid, (event.raw_text or "").strip())
        except Exception:
            log.exception("handler failed")
            await event.reply("⚠️ Something went wrong. Please try again.")

    async def on_file(self, event, uid):
        if not self.cfg.may_upload(uid):
            return await event.reply("🚫 You are not allowed to upload.")
        if event.sticker or event.video_note:
            return await event.reply("ℹ️ Send this as a file (document) to store it.")
        ext = event.file.ext or ""
        name = core.clean_name(event.file.name or f"file_{event.id}{ext}")
        size = event.file.size or 0
        async with self.albums:
            if uid in self.batches:
                batch = self.batches[uid]
                if len(batch) >= 100:
                    return await event.reply("Limit is 100 parts. Send /done.")
                batch.append((event.media, name, size))
                return await event.reply(f"➕ Part {len(batch)} added: {html.escape(name)}\nSend /done when finished.")
        meta = base_meta(uid, self.names.get(uid), name, size)
        await self.store.save_reference(event.media, meta)
        await self.show(event.chat_id, self.store.index.get(meta["k"]), uid)

    async def finish_batch(self, event, uid):
        batch = self.batches.pop(uid, None)
        if not batch:
            return await event.reply("Nothing to save. Start with /multi.")
        split = core.detect_split([b[1] for b in batch])
        if split:
            title, order = split
            batch = [batch[i] for i in order]
        else:
            title = batch[0][1] if len(batch) == 1 else f"{len(batch)} files - {batch[0][1]}"
        key, saved = core.new_key(), []
        try:
            for i, (media, name, size) in enumerate(batch):
                meta = base_meta(uid, self.names.get(uid), title, size, key, i, len(batch),
                                 combined=bool(split) or len(batch) == 1)
                meta["p"] = name
                saved.append((await self.store.save_reference(media, meta)).id)
        except Exception:
            if saved:
                await self.client.delete_messages(self.store.channel, saved)
            self.store.index.remove(key)
            self.batches[uid] = batch
            raise
        await self.show(event.chat_id, self.store.index.get(key), uid)

    async def on_command(self, event, uid, text):
        words = text.split(maxsplit=2)
        if not words:
            return
        cmd = words[0].split("@")[0].lower()
        arg = words[1] if len(words) > 1 else ""
        rest = words[2] if len(words) > 2 else ""

        if cmd == "/start" and arg:
            return await self.deliver(event.chat_id, uid, arg)
        if cmd in ("/start", "/help"):
            return await event.reply(
                WELCOME.format(max=core.human(self.cfg.max_bytes)),
                buttons=[[Button.url("📊 Live dashboard", self.cfg.base_url)]]
                if self.cfg.base_url.startswith("https://") else None)
        if cmd == "/id":
            return await event.reply(f"🆔 <code>{uid}</code>")
        if cmd == "/upload":
            if not self.cfg.may_upload(uid):
                return await event.reply("🚫 You are not allowed to upload.")
            token = core.sign(self.cfg.secret, {"up": uid}, 12 * 3600)
            return await event.reply(
                f"🌐 Your private upload page (valid 12h, do not share):\n"
                f"{self.cfg.base_url}/upload#{token}", link_preview=False)
        if cmd == "/multi":
            if not self.cfg.may_upload(uid):
                return await event.reply("🚫 You are not allowed to upload.")
            self.batches[uid] = []
            return await event.reply(
                "🧩 Send the parts now (e.g. movie.mkv.001, .002 …), then /done.\n/cancel to stop.")
        if cmd == "/done":
            return await self.finish_batch(event, uid)
        if cmd == "/cancel":
            self.batches.pop(uid, None)
            return await event.reply("Cancelled.")
        if cmd == "/files":
            mine = self.store.index.owned_by(uid)
            if not mine:
                return await event.reply("📭 No files yet. Send me one.")
            lines = [f"{'🔒' if r.allowed else '📁'} {html.escape(r.name[:40])} — "
                     f"{core.human(r.size)}\n<code>{r.key}</code>" for r in mine[:25]]
            more = f"\n\n…and {len(mine) - 25} older" if len(mine) > 25 else ""
            return await event.reply("🗂 <b>Your files</b>\n\n" + "\n\n".join(lines) + more)
        if cmd == "/stats" and uid in self.cfg.admins:
            count, size = self.store.index.totals()
            return await event.reply(
                f"📊 Files: {count}\n💾 {core.human(size)}\n⏱ {core.duration(time.time() - self.started)}")
        if cmd == "/reindex" and uid in self.cfg.admins:
            await self.store.rebuild()
            return await event.reply(f"✅ Indexed {self.store.index.totals()[0]} files.")
        if cmd == "/get":
            return await self.deliver(event.chat_id, uid, arg)

        owner_cmds = ("/rename", "/allow", "/deny", "/public", "/newkey", "/delete")
        if cmd in ("/info", "/link") + owner_cmds:
            rec = self.store.index.get(arg)
            if not rec or not (rec.can_access(uid) or uid in self.cfg.admins):
                return await event.reply("❌ Invalid key or no access.")
            if cmd in ("/info", "/link"):
                return await self.show(event.chat_id, rec, uid)
            if uid != rec.owner and uid not in self.cfg.admins:
                return await event.reply("🚫 Only the owner can do that.")
            if cmd == "/delete":
                await self.store.delete(rec)
                return await event.reply("🗑 Deleted.")
            if cmd == "/rename":
                if not rest:
                    return await event.reply("Usage: /rename KEY new name")
                await self.store.update(rec, name=core.clean_name(rest))
            elif cmd in ("/allow", "/deny"):
                if not rest.isdigit():
                    return await event.reply(f"Usage: {cmd} KEY USER_ID")
                target = int(rest)
                allowed = [x for x in rec.allowed if x != target]
                if cmd == "/allow":
                    if len(allowed) >= core.MAX_ALLOWED:
                        return await event.reply(f"Maximum {core.MAX_ALLOWED} users per file.")
                    allowed.append(target)
                elif not allowed and rec.allowed:
                    allowed = [rec.owner]     # stay private after removing the last user
                await self.store.update(rec, allowed=allowed)
            elif cmd == "/public":
                await self.store.update(rec, allowed=[])
            elif cmd == "/newkey":
                await self.store.update(rec, key=core.new_key())
            return await self.show(event.chat_id, rec, uid)

        # a bare key pasted into chat
        if self.store.index.get(text):
            return await self.deliver(event.chat_id, uid, text)
        await event.reply("🤔 Send a file or a key. /help")

    async def on_button(self, event):
        action, _, key = event.data.decode().partition(":")
        uid = event.sender_id
        rec = self.store.index.get(key)
        if not self.store.ready or not rec or not rec.can_access(uid):
            return await event.answer("Not available.", alert=True)
        if action == "get":
            await event.answer("Sending…")
            return await self.deliver(event.chat_id, uid, key)
        if uid != rec.owner and uid not in self.cfg.admins:
            return await event.answer("Only the owner can do that.", alert=True)
        if action == "ask":
            await event.answer()
            return await event.respond(
                "Delete this file permanently?",
                buttons=[[Button.inline("Yes, delete", f"del:{key}".encode())]])
        if action == "del":
            await self.store.delete(rec)
            await event.edit("🗑 Deleted.")

    # ----- files posted straight into the storage channel -----
    async def on_channel(self, event):
        msg = event.message
        if (not self.store.ready or msg.out or not msg.file or msg.web_preview
                or msg.sticker or msg.video_note
                or core.parse_caption(msg.raw_text)):
            return
        # The bot's own channel posts always carry a tag, so they are skipped above.
        owner = min(self.cfg.admins)
        meta = base_meta(owner, "channel", msg.file.name or f"file_{msg.id}{msg.file.ext or ''}",
                         msg.file.size or 0)
        try:
            await self.client.edit_message(self.store.channel, msg.id,
                                           core.make_caption(meta), parse_mode=None)
            self.store.index.add(msg.id, meta)
        except Exception:
            # forwarded posts cannot be edited: store a tagged copy instead
            await self.store.save_reference(msg.media, meta)
        try:
            await self.show(owner, self.store.index.get(meta["k"]), owner)
        except Exception:
            log.warning("could not message admin %s (send /start to the bot first)", owner)

    async def on_deleted(self, event):
        for msg_id in event.deleted_ids:
            self.store.index.remove_message(msg_id)

    async def notify(self, uid, text, message=None):
        """Send or edit a progress message; never let Telegram errors break uploads."""
        try:
            if message is None:
                return await self.client.send_message(uid, text, parse_mode=None)
            await message.edit(text, parse_mode=None)
        except Exception as e:
            log.debug("notify failed: %s", e)
        return message
