import asyncio
import logging

from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import MemorySession

from app.bot import Bot
from app.config import Config
from app.store import Store
from app.web import Web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main():
    cfg = Config()
    client = TelegramClient(MemorySession(), cfg.api_id, cfg.api_hash,
                            flood_sleep_threshold=120, connection_retries=None,
                            auto_reconnect=True)
    store = Store(client, cfg)
    bot = Bot(client, store, cfg)
    site = Web(store, bot, cfg, client)

    # Open the port first so the host's health check passes while we index.
    runner = web.AppRunner(site.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    logging.info("dashboard on port %s", cfg.port)

    tasks = []
    try:
        await client.start(bot_token=cfg.bot_token)
        await bot.start()
        await store.start()
        tasks.append(asyncio.create_task(site.cleanup()))
        if cfg.keep_alive:
            tasks.append(asyncio.create_task(site.keep_alive()))
        logging.info("bot @%s is running", bot.username)
        await client.run_until_disconnected()
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
