# Local Telegram Bot API Server

The Local Bot API server is separate from the web app and downloader bot. It has its own `telegram_api_data` Docker volume and only the bot can access it through Docker's internal network. No host port is exposed.

## Required `.env` values

Add these values yourself; never commit or paste them in chat:

```env
TELEGRAM_BOT_TOKEN=your-new-bot-token
TELEGRAM_FULL_BOT_TOKEN=your-second-bot-token
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

## Start the isolated stack

```powershell
docker compose -f docker-compose.yml -f docker-compose.local-api.yml pull
docker compose -f docker-compose.yml -f docker-compose.local-api.yml up --build -d
```

The stack uses the prebuilt `aiogram/telegram-bot-api` image (a maintained image of Telegram's open-source Bot API server), so no C++/TDLib compilation runs on the VPS. `--build` only rebuilds the lightweight Python bot image after source changes; it does not compile C++/TDLib. The regular `docker-compose.yml` retains its existing behavior; include the second compose file only for large Telegram uploads.

## One-time migration from the cloud Bot API

Before starting the local server for the first time, log the bot out from the cloud endpoint. This is required by Telegram when switching to a Local Bot API server. Run this on the VPS from the project directory; the token stays in `.env` and is not placed in shell history:

```bash
set -a; . ./.env; set +a
curl --fail-with-body "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut"
```

After it returns `"ok":true`, start the stack above. The project deliberately uses a **different token** for `telegram-full-bot`: Telegram doesn't allow one token to be logged in on Cloud and Local Bot API at the same time.

`telegram-bot` uses Cloud Bot API and sends episodes up to 49 MB. `telegram-full-bot` uses `http://telegram-api:8081` internally and only offers FULL uploads up to 1.9 GB. Keep enough free disk: a 600 MB FULL can temporarily use more than 1.2 GB while its episode files and final MP4 coexist.
