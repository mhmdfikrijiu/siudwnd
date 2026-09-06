from __future__ import annotations

import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.parse import urlparse

import nartodrama_downloader as downloader

_SLUG = re.compile(r"^[a-z0-9-]+$")
ProgressCallback = Callable[[str, int | None, int, int], None]


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    """The user explicitly stopped this download job."""

    pass


def validate_source_url(url: str) -> str:
    """Accept only the source currently supported by this bot."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"narto-drama.com", "www.narto-drama.com"}:
        raise DownloadError("Kirim link HTTPS dari narto-drama.com.")
    slug = downloader.extract_slug_from_url(url)
    if not _SLUG.fullmatch(slug):
        raise DownloadError("Slug drama tidak valid.")
    return slug


def resolve(url: str) -> tuple[str, str, str, list[dict]]:
    slug = validate_source_url(url)
    episodes, title, poster = downloader.fetch_episode_list(slug)
    clean = [
        {"number": item.get("route_episode_number") or item.get("number"), "title": item.get("title") or "Episode"}
        for item in episodes
    ]
    clean = [item for item in clean if isinstance(item["number"], int)]
    if not clean:
        raise DownloadError("Episode tidak ditemukan pada link tersebut.")
    return slug, title or slug, poster or "", sorted(clean, key=lambda item: item["number"])


def download_one(slug: str, episode: int, work_dir: Path, cancel_event: Event | None = None) -> Path:
    """Download into a unique per-request directory; caller must delete it after upload."""
    job_dir = work_dir / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    destination = job_dir / f"{slug}-Ep{episode:02d}.mp4"
    try:
        _download_episode_to_path(slug, episode, destination, cancel_event=cancel_event)
        return destination
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def download_full(
    slug: str,
    episode_numbers: list[int],
    work_dir: Path,
    workers: int = 2,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> Path:
    """Download all requested episodes in a bounded parallel pool, then remux one MP4."""
    job_dir = work_dir / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    numbers = sorted(set(episode_numbers))
    if len(numbers) < 2:
        raise DownloadError("FULL membutuhkan minimal dua episode.")
    try:
        workers = max(1, min(workers, 3, len(numbers)))
        failures: list[int] = []
        paths: dict[int, Path] = {}

        def report(stage: str, episode: int | None = None) -> None:
            if progress_callback:
                try:
                    progress_callback(stage, episode, len(paths), len(numbers))
                except Exception:
                    pass

        def one(number: int) -> tuple[int, Path]:
            path = job_dir / f"Ep{number:02d}.mp4"
            _download_episode_to_path(slug, number, path, lambda: report("retry", number), cancel_event)
            return number, path

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(one, number) for number in numbers]
            for future in as_completed(futures):
                if cancel_event and cancel_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise DownloadCancelled("Download dihentikan.")
                try:
                    number, path = future.result()
                    paths[number] = path
                    report("done", number)
                except Exception:
                    failures.append(0)
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Download dihentikan.")
        if failures or len(paths) != len(numbers):
            raise DownloadError("Ada episode yang gagal diunduh; FULL tidak dibuat agar hasil tidak terpotong.")
        report("concat")
        output = job_dir / f"{slug}-FULL-Ep{numbers[0]:02d}-{numbers[-1]:02d}.mp4"
        if not downloader.concat_mp4s([str(paths[number]) for number in numbers], str(output), cancel_event):
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled("Download dihentikan.")
            raise DownloadError("Gagal menggabungkan episode menjadi FULL MP4.")
        return output
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def _download_episode_to_path(
    slug: str, episode: int, destination: Path, on_retry: Callable[[], None] | None = None,
    cancel_event: Event | None = None,
) -> None:
    """Retry source failures and refresh an expired HLS URL before giving up."""
    if shutil.which("ffmpeg") is None:
        raise DownloadError(
            "ffmpeg belum terpasang atau belum masuk PATH komputer. "
            "Install ffmpeg, lalu tutup dan buka ulang terminal sebelum menjalankan bot."
        )
    last_error: Exception | None = None
    referer = f"{downloader.BASE_SITE}/detail/watch/{slug}/{episode}?lang=id-ID"
    for attempt in range(3):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Download dihentikan.")
        try:
            episodes, _, _ = downloader.fetch_episode_list(slug)
            target = next(
                (item for item in episodes if (item.get("route_episode_number") or item.get("number")) == episode),
                None,
            )
            if not target:
                raise DownloadError(f"Episode {episode} tidak tersedia.")
            referer = target.get("watch_url") or referer
            # CDN links frequently reject the URL embedded in a cached episode
            # list. Fetch a page-specific URL before invoking ffmpeg, so we do
            # not spend one full attempt on a predictable 403 response.
            cached_url = (target.get("direct_play_url") or target.get("play_url") or "").strip()
            play_url = downloader._fetch_fresh_url_from_page(slug, episode) or cached_url
            if play_url and downloader.download_episode_hls(play_url, str(destination), referer, cancel_event):
                if downloader.is_valid_mp4(str(destination)):
                    return
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled("Download dihentikan.")
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            last_error = DownloadError("server video belum merespons")
        except Exception as error:
            last_error = error
        if attempt < 2:
            if on_retry:
                on_retry()
            # Exponential backoff reduces repeated 502 requests to an overloaded source.
            if cancel_event and cancel_event.wait(2 ** (attempt + 1)):
                raise DownloadCancelled("Download dihentikan.")
    raise DownloadError(
        f"Episode {episode} belum bisa diambil dari server sumber setelah 3 percobaan. "
        "Coba lagi 5–10 menit lagi."
    ) from last_error


def remove_job_file(path: Path) -> None:
    """Delete the entire request directory after Telegram has consumed the upload."""
    shutil.rmtree(path.parent, ignore_errors=True)


def purge_work_dir(work_dir: Path, max_age_seconds: int = 3600) -> int:
    """Recover abandoned jobs from restarts or interrupted uploads."""
    import time
    removed = 0
    if not work_dir.exists():
        return removed
    cutoff = time.time() - max_age_seconds
    for item in work_dir.iterdir():
        try:
            if item.is_dir() and item.stat().st_mtime < cutoff:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
