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
docker compose -f docker-compose.yml -f docker-compose.local-api.yml up --build -d
```

The first build compiles Telegram's official Bot API server and can take several minutes. The regular `docker-compose.yml` retains its existing behavior; include the second compose file only for large Telegram uploads.

The override makes the downloader bot use `http://telegram-api:8081` internally and permits files up to 1.9 GB. Keep enough free disk: a 600 MB FULL can temporarily use more than 1.2 GB while its episode files and final MP4 coexist.
