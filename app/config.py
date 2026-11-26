import hashlib
import os
import sys


def _ids(name):
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}


class Config:
    def __init__(self):
        missing = [v for v in ("API_ID", "API_HASH", "BOT_TOKEN", "CHANNEL_ID", "ADMIN_IDS")
                   if not os.getenv(v)]
        if missing:
            sys.exit("Missing environment variables: " + ", ".join(missing))
        self.api_id = int(os.environ["API_ID"])
        self.api_hash = os.environ["API_HASH"]
        self.bot_token = os.environ["BOT_TOKEN"]
        self.channel_id = int(os.environ["CHANNEL_ID"])
        self.admins = _ids("ADMIN_IDS")
        self.uploaders = _ids("ALLOWED_UPLOADERS")      # empty = everyone
        self.secret = os.getenv("SECRET_KEY") or hashlib.sha256(
            ("tgf:" + self.bot_token).encode()).hexdigest()
        self.base_url = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
                         or "http://localhost:8080").rstrip("/")
        self.port = int(os.getenv("PORT", "8080"))
        self.max_bytes = int(float(os.getenv("MAX_FILE_GB", "10")) * 1024 ** 3)
        self.max_uploads = int(os.getenv("MAX_WEB_UPLOADS", "3"))
        self.max_downloads = int(os.getenv("MAX_WEB_DOWNLOADS", "5"))
        self.bandwidth_limit = float(os.getenv("BANDWIDTH_LIMIT_GB", "5"))
        self.keep_alive = os.getenv("KEEP_ALIVE", "0") == "1"

    def may_upload(self, uid):
        return not self.uploaders or uid in self.uploaders or uid in self.admins
