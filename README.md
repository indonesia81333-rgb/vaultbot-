# Cloud File Vault: Telegram file-store bot

Store files in your private Telegram channel and share them with a secret key.
Includes web upload/download pages and a live status dashboard.

## Features
- Send a file to the bot and get a key instantly (Telegram-side copy, no re-upload)
- Web upload up to 10 GB with progress bar, speed, ETA, pause and retry
- Files over about 1.9 GB are split into parts automatically and downloaded as **one file** from the web link
- `/multi` + `/done` groups your own `.001 .002` or `.part001` pieces into one file
- Files posted directly in the storage channel get a key automatically
- Private files: allow specific user IDs, remove them, or replace the key
- No database: the channel is the backup, and the index is rebuilt on every start

## Real limits (read first)
| Path | Max size | Uses host bandwidth |
|---|---|---|
| Send file to bot / get by key | 2 GB per file (Telegram) | No |
| Web upload / download | 10 GB (`MAX_FILE_GB`) | **Yes** |

- A bot cannot send a single file larger than 2000 MB. Larger files arrive in Telegram as
  parts (`name.001`, `name.002`). Join with 7-Zip, `cat name.* > name`, or use the web link.
- Premium users can send up to 4 GB files to Telegram. Web download of such a file is untested.
- Speed depends on your host CPU, network and Telegram. "Instant" applies only to Telegram-side copies.
- **Render Hobby includes 5 GB outbound bandwidth per month** (checked 26 Nov 2026,
  https://render.com/docs/outbound-bandwidth). Web uploads (server to Telegram) and web downloads
  (server to user) both count. Extra is billed ($0.15/GB) if a card is on file; otherwise free
  services are suspended. For regular large web transfers use a VPS or another host with more traffic.
- Render free services sleep after 15 minutes without incoming traffic and may restart at any time.
  A sleeping bot does not answer. Options: a paid instance (best), an external uptime monitor on
  `/health`, or `KEEP_ALIVE=1`. Check Render's current terms before using keep-alive.
- A restart cancels unfinished web uploads. Leftover parts of those stay in the channel but are ignored.

## Setup
1. `API_ID` and `API_HASH`: https://my.telegram.org -> API development tools.
2. `BOT_TOKEN`: create a bot with @BotFather.
3. Create a **private** channel. Add the bot as admin with post, edit and delete permissions.
4. `CHANNEL_ID`: the channel id, starting with `-100` (forward a channel post to an ID bot,
   or copy a post link `t.me/c/1234567890/5` and use `-1001234567890`).
5. `ADMIN_IDS`: your numeric user id. Send `/start` to your bot once.
6. If the logs say the storage channel cannot be opened, check the id and admin rights, then
   post any message in the channel. The bot retries every 15 seconds.

## Deploy on Render
1. Unzip, create a GitHub repository, and upload all files (never upload `.env`).
2. Render -> New -> Blueprint -> choose the repository.
3. Enter the environment variables when asked and deploy.
4. Open your service URL for the dashboard.

Run only **one** instance per bot token.

## Run locally
```bash
pip install -r requirements.txt
set -a; source .env; set +a      # Linux/macOS
python main.py                   # http://localhost:8080
python -m unittest discover tests
```

## Commands
`/start` `/help` `/upload` `/multi` `/done` `/cancel` `/files` `/get` `/info` `/link`
`/rename` `/allow` `/deny` `/public` `/newkey` `/delete` `/id` ; admin: `/stats` `/reindex`

## Security
- Anyone holding a public file's key can download it. Use `/allow` for private files.
- Keys are visible in the storage channel captions, so keep the channel private.
- By default **anyone** can upload into your channel. Set `ALLOWED_UPLOADERS` to restrict it.
- `/newkey`, `/deny` and `/delete` cannot recall copies people already received.
- Host only content you have the right to share.
