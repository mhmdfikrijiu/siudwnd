# Local Telegram Bot API Server

The Local Bot API server is separate from the web app and downloader bot. It has its own `telegram_api_data` Docker volume and only the bot can access it through Docker's internal network. No host port is exposed.

## Required `.env` values

Add these values yourself; never commit or paste them in chat:

```env
TELEGRAM_BOT_TOKEN=your-new-bot-token
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

## Start the isolated stack

```powershell
docker compose -f docker-compose.yml -f docker-compose.local-api.yml pull
docker compose -f docker-compose.yml -f docker-compose.local-api.yml up -d
```

The stack uses the prebuilt `aiogram/telegram-bot-api` image (a maintained image of Telegram's open-source Bot API server), so no C++/TDLib compilation runs on the VPS. The regular `docker-compose.yml` retains its existing behavior; include the second compose file only for large Telegram uploads.

## One-time migration from the cloud Bot API

Before starting the local server for the first time, log the bot out from the cloud endpoint. This is required by Telegram when switching to a Local Bot API server. Run this on the VPS from the project directory; the token stays in `.env` and is not placed in shell history:

```bash
set -a; . ./.env; set +a
curl --fail-with-body "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut"
```

After it returns `"ok":true`, start the stack above. Do not run two bot containers with the same token at the same time.

The override makes the downloader bot use `http://telegram-api:8081` internally and permits files up to 1.9 GB. Keep enough free disk: a 600 MB FULL can temporarily use more than 1.2 GB while its episode files and final MP4 coexist.
