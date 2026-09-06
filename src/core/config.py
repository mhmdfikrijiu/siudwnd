from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Docker Compose already injects .env into the bot container. Loading it here
# additionally makes `python -m src.bot.main` work from the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    work_dir: Path
    max_upload_bytes: int
    max_concurrent_downloads: int
    job_ttl_seconds: int
    api_base_url: str | None

    @classmethod
    def from_env(cls) -> "TelegramSettings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN belum diatur. Salin .env.example menjadi .env.")
        raw_work_dir = os.environ.get("TELEGRAM_WORK_DIR", "./runtime/telegram_jobs")
        # `/data/...` is the Docker path. When a copied Docker .env is used
        # directly on Windows, keep temporary files inside this project instead.
        if os.name == "nt" and raw_work_dir.replace("\\", "/").startswith("/data/"):
            work_dir = PROJECT_ROOT / "runtime" / Path(raw_work_dir).name
        else:
            work_dir = Path(raw_work_dir)
        max_upload_mb = max(1, int(os.environ.get("TELEGRAM_MAX_UPLOAD_MB", "45")))
        concurrency = max(1, min(4, int(os.environ.get("TELEGRAM_MAX_CONCURRENT_DOWNLOADS", "2"))))
        job_ttl_minutes = max(30, int(os.environ.get("TELEGRAM_JOB_TTL_MINUTES", "180")))
        api_base_url = os.environ.get("TELEGRAM_API_BASE_URL", "").strip().rstrip("/") or None
        return cls(token, work_dir, max_upload_mb * 1024 * 1024, concurrency, job_ttl_minutes * 60, api_base_url)
