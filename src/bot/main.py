from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from threading import Event

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from src.core.config import TelegramSettings
from src.services.telegram_downloads import DownloadCancelled, DownloadError, download_full, download_one, purge_work_dir, remove_job_file, resolve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger(__name__)
# httpx logs request URLs at INFO level; Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass
class DramaSelection:
    slug: str
    title: str
    poster: str
    episodes: list[dict]


@dataclass
class ActiveJob:
    cancel_event: Event
    task: asyncio.Task


def job_key(update: Update) -> tuple[int, int]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        raise RuntimeError("Chat atau pengguna tidak tersedia.")
    return chat.id, user.id


def episode_keyboard(episodes: list[dict], *, full_only: bool = False) -> InlineKeyboardMarkup:
    if full_only:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🎬 FULL — gabung semua episode", callback_data="full")]])
    buttons = [InlineKeyboardButton(f"Ep {item['number']:02d}", callback_data=f"ep:{item['number']}") for item in episodes]
    rows = [buttons[index : index + 4] for index in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("📤 Download & kirim semua (satu per satu)", callback_data="all")])
    rows.append([InlineKeyboardButton("🎬 FULL — gabung semua episode", callback_data="full")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    full_only = context.application.bot_data["full_only"]
    if full_only:
        await update.effective_message.reply_text(
            "🎬 Narto FULL Downloader\n\nKirim link drama dari narto-drama.com. Bot ini hanya membuat satu video FULL "
            "dan dapat mengirim file besar hingga 1,9 GB."
        )
        return
    await update.effective_message.reply_text(
        "🎬 Narto Downloader\n\nKirim link drama dari narto-drama.com. Setelah itu pilih episode yang ingin dikirim."
        "\n\nFile hanya disimpan sementara selama proses kirim, lalu otomatis dihapus.",
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs: dict[tuple[int, int], ActiveJob] = context.application.bot_data["active_jobs"]
    active = jobs.get(job_key(update))
    if active is None or active.task.done():
        await update.effective_message.reply_text("Tidak ada proses download atau upload yang sedang berjalan.")
        return
    active.cancel_event.set()
    active.task.cancel()
    await update.effective_message.reply_text(
        "⏹ Proses dihentikan. Download/unggahan yang sedang berjalan dibatalkan dan file sementara akan dibersihkan."
    )


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    status = await update.effective_message.reply_text("🔎 Mengecek daftar episode…")
    try:
        slug, title, poster, episodes = await asyncio.to_thread(resolve, text)
        context.user_data["drama"] = DramaSelection(slug, title, poster, episodes)
        full_only = context.application.bot_data["full_only"]
        detail = (
            "🎬 Tekan tombol FULL untuk menggabungkan seluruh episode menjadi satu MP4 besar."
            if full_only
            else (
                "📥 Pilih satu episode, atau pilih ‘Download & kirim semua’ untuk memproses seluruh episode "
                "berurutan: download → kirim → hapus file → lanjut episode berikutnya."
            )
        )
        card_text = (
            f"🎬 {title}\n\n"
            f"📚 {len(episodes)} episode tersedia\n"
            f"{detail}\n\n"
            "ℹ️ File dibersihkan otomatis setelah berhasil dikirim."
        )
        keyboard = episode_keyboard(episodes, full_only=full_only)
        if poster:
            try:
                await update.effective_message.reply_photo(
                    photo=poster,
                    caption=card_text,
                    reply_markup=keyboard,
                )
                await status.delete()
                return
            except Exception as error:
                LOG.info("poster unavailable: %s", error)
        await status.edit_text(card_text, reply_markup=keyboard)
    except Exception as error:
        LOG.info("resolve failed: %s", error)
        await status.edit_text(f"⚠️ {error}")


async def edit_status(query, text: str) -> None:
    """Update either a text card or a cover-photo card in place."""
    if query.message and query.message.photo:
        await query.edit_message_caption(caption=text)
    else:
        await query.edit_message_text(text)


async def safe_edit_status(query, text: str) -> None:
    """Status text must never make a completed file delivery look failed."""
    try:
        await edit_status(query, text)
    except TimedOut:
        LOG.warning("Telegram timed out while updating the status card")
    except Exception as error:
        LOG.warning("could not update the status card: %s", error)


async def send_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    jobs: dict[tuple[int, int], ActiveJob] = context.application.bot_data["active_jobs"]
    key = job_key(update)
    current_task = asyncio.current_task()
    if current_task is None:
        raise RuntimeError("Task Telegram tidak tersedia.")
    if key in jobs and not jobs[key].task.done():
        await safe_edit_status(query, "⏳ Masih ada proses berjalan. Gunakan /stop untuk membatalkannya terlebih dahulu.")
        return
    active_job = ActiveJob(Event(), current_task)
    jobs[key] = active_job
    selection: DramaSelection | None = context.user_data.get("drama")
    if not selection:
        await edit_status(query, "Sesi sudah berakhir. Kirim link drama lagi.")
        return
    is_full = query.data == "full"
    is_all = query.data == "all"
    if context.application.bot_data["full_only"] and not is_full:
        await edit_status(query, "Bot ini hanya menangani tombol FULL. Kirim link lagi bila sesi sudah berakhir.")
        return
    episode = None if is_full or is_all else int(query.data.split(":", 1)[1])
    settings: TelegramSettings = context.application.bot_data["settings"]
    semaphore: asyncio.Semaphore = context.application.bot_data["download_semaphore"]
    event_loop = asyncio.get_running_loop()
    progress = {"done": set(), "retry": set(), "stage": "download"}

    def progress_text() -> str:
        done = sorted(progress["done"])
        retries = sorted(progress["retry"])
        total = len(selection.episodes)
        if progress["stage"] == "concat":
            return (
                f"🔗 Menggabungkan FULL MP4\n\n🎬 {selection.title}\n"
                f"✅ Semua {total}/{total} episode selesai\n"
                "⏳ Menyatukan video tanpa re-encode…"
            )
        completed = ", ".join(f"Ep {number:02d}" for number in done) or "Belum ada"
        retry_line = f"\n⚠️ Retry: {', '.join(f'Ep {number:02d}' for number in retries)}" if retries else ""
        return (
            f"⏳ Menyiapkan FULL MP4\n\n🎬 {selection.title}\n"
            f"📥 Mengunduh episode: {len(done)}/{total}\n"
            f"✅ Selesai: {completed}{retry_line}\n"
            f"📊 Progress: {int(len(done) / total * 100)}%\n\n"
            "Tahap berikutnya: gabungkan video → kirim ke Telegram"
        )

    def on_full_progress(stage: str, number: int | None, _done: int, _total: int) -> None:
        if active_job.cancel_event.is_set():
            return
        if stage == "done" and number is not None:
            progress["done"].add(number)
            progress["retry"].discard(number)
        elif stage == "retry" and number is not None:
            progress["retry"].add(number)
        elif stage == "concat":
            progress["stage"] = "concat"
        future = asyncio.run_coroutine_threadsafe(edit_status(query, progress_text()), event_loop)
        future.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    if is_all:
        await edit_status(query,
            f"📤 Kirim Semua — per episode\n\n🎬 {selection.title}\n📚 0/{len(selection.episodes)} terkirim\n"
            "Episode dikirim berurutan. Bot menyiapkan maksimal dua episode berikutnya di latar belakang."
        )
        sent: list[int] = []
        failed: list[int] = []
        prefetch_limit = min(2, settings.max_concurrent_downloads)
        pending_downloads: dict[int, asyncio.Task] = {}
        next_episode_index = 0

        def prefetch_more() -> None:
            nonlocal next_episode_index
            while len(pending_downloads) < prefetch_limit and next_episode_index < len(selection.episodes):
                item = selection.episodes[next_episode_index]
                next_episode_index += 1
                number = item["number"]
                pending_downloads[number] = asyncio.create_task(
                    asyncio.to_thread(download_one, selection.slug, number, settings.work_dir, active_job.cancel_event),
                    name=f"download-episode-{number}",
                )

        try:
            async with semaphore:
                prefetch_more()
                for index, item in enumerate(selection.episodes, start=1):
                    if active_job.cancel_event.is_set():
                        raise DownloadCancelled("Download dihentikan.")
                    number = item["number"]
                    path = None
                    try:
                        await edit_status(query,
                            f"📥 Menyiapkan Episode {number:02d}\n\n🎬 {selection.title}\n"
                            f"📤 Terkirim: {len(sent)}/{len(selection.episodes)}\n"
                            f"⚡ Prefetch: hingga {prefetch_limit} episode berikutnya"
                        )
                        download_started = time.monotonic()
                        path = await pending_downloads.pop(number)
                        prefetch_more()
                        size = path.stat().st_size
                        LOG.info(
                            "send-all download ready: %s (%.1f MB) in %.1fs",
                            path.name,
                            size / 1024 / 1024,
                            time.monotonic() - download_started,
                        )
                        if size > settings.max_upload_bytes:
                            raise DownloadError(
                                f"Ep {number:02d} ({size / 1024 / 1024:.1f} MB) melewati batas upload bot."
                            )
                        await edit_status(query,
                            f"📤 Mengirim Episode {number:02d}\n\n🎬 {selection.title}\n"
                            f"📦 {size / 1024 / 1024:.1f} MB"
                        )
                        upload_started = time.monotonic()
                        LOG.info("send-all starting Telegram upload: %s (%.1f MB)", path.name, size / 1024 / 1024)
                        try:
                            with path.open("rb") as file_handle:
                                await query.message.reply_document(
                                    document=file_handle,
                                    filename=path.name,
                                    caption=f"✅ {selection.title}\nEpisode {number:02d} · {size / 1024 / 1024:.1f} MB",
                                )
                        except TimedOut:
                            # Never retry automatically: the file may already be in chat.
                            LOG.warning(
                                "send-all upload response timed out for %s after %.1fs",
                                path.name,
                                time.monotonic() - upload_started,
                            )
                            failed.append(number)
                            continue
                        LOG.info(
                            "send-all Telegram upload confirmed: %s in %.1fs",
                            path.name,
                            time.monotonic() - upload_started,
                        )
                        sent.append(number)
                    except Exception as error:
                        failed.append(number)
                        LOG.warning("episode %s skipped in send-all: %s", number, error)
                    finally:
                        if path:
                            await asyncio.to_thread(remove_job_file, path)
            sent_text = ", ".join(f"Ep {number:02d}" for number in sent) or "tidak ada"
            failed_text = f"\n⚠️ Gagal: {', '.join(f'Ep {number:02d}' for number in failed)}" if failed else ""
            await edit_status(query,
                f"✅ Kirim Semua selesai\n\n🎬 {selection.title}\n"
                f"📤 Terkirim ({len(sent)}/{len(selection.episodes)}): {sent_text}{failed_text}\n\n"
                "File sementara di server telah dibersihkan."
            )
        except DownloadCancelled:
            await safe_edit_status(query, "⏹ Kirim semua dihentikan. File sementara sedang dibersihkan.")
        except asyncio.CancelledError:
            await safe_edit_status(query, "⏹ Kirim semua dihentikan. File sementara sedang dibersihkan.")
            raise
        except Exception as error:
            LOG.exception("send-all failed")
            await edit_status(query, f"⚠️ Kirim semua berhenti: {error}")
        finally:
            # On cancellation/failure, wait for prefetches to finish and remove
            # their temporary files so they cannot accumulate on the VPS.
            for task in pending_downloads.values():
                try:
                    leftover = await task
                    await asyncio.to_thread(remove_job_file, leftover)
                except Exception:
                    pass
            if jobs.get(key) is active_job:
                jobs.pop(key, None)
        return
    if is_full:
        await edit_status(query, progress_text())
    else:
        await edit_status(query, f"⏳ Mengunduh\n\n🎬 {selection.title}\n📺 Episode {episode:02d}")
    path = None
    download_started = time.monotonic()
    try:
        async with semaphore:
            if is_full:
                path = await asyncio.to_thread(
                    download_full, selection.slug,
                    [item["number"] for item in selection.episodes], settings.work_dir,
                    settings.max_concurrent_downloads, on_full_progress, active_job.cancel_event,
                )
            else:
                path = await asyncio.to_thread(download_one, selection.slug, episode, settings.work_dir, active_job.cancel_event)
        size = path.stat().st_size
        LOG.info(
            "download ready: %s (%.1f MB) in %.1fs",
            path.name,
            size / 1024 / 1024,
            time.monotonic() - download_started,
        )
        if size > settings.max_upload_bytes:
            raise DownloadError(f"File {size / 1024 / 1024:.1f} MB melebihi batas bot ({settings.max_upload_bytes / 1024 / 1024:.0f} MB).")
        if is_full:
            await edit_status(query,
                f"📤 Mengunggah FULL ke Telegram\n\n🎬 {selection.title}\n"
                f"📦 {size / 1024 / 1024:.1f} MB\n⏳ Jangan tekan tombol lagi sampai file muncul."
            )
        upload_started = time.monotonic()
        LOG.info("starting Telegram upload: %s (%.1f MB)", path.name, size / 1024 / 1024)
        try:
            with path.open("rb") as file_handle:
                await query.message.reply_document(
                    document=file_handle,
                    filename=path.name,
                    caption=(f"✅ {selection.title}\nFULL · {len(selection.episodes)} episode · {size / 1024 / 1024:.1f} MB"
                             if is_full else f"✅ {selection.title}\nEpisode {episode:02d} · {size / 1024 / 1024:.1f} MB"),
                )
        except TimedOut:
            # Telegram can finish a delivery before its API response reaches us.
            # Never retry automatically: that could create a duplicate document.
            LOG.warning("Telegram upload response timed out for %s after %.1fs", path.name, time.monotonic() - upload_started)
            await safe_edit_status(
                query,
                "⏱ Telegram belum mengonfirmasi pengiriman. Jika file sudah muncul, proses berhasil. "
                "Jika belum muncul dalam 1 menit, tekan tombol episode sekali lagi.",
            )
            return
        LOG.info("Telegram upload confirmed: %s in %.1fs", path.name, time.monotonic() - upload_started)
        await safe_edit_status(query, "✅ File terkirim dan salinan sementara di server sudah dibersihkan.")
    except DownloadCancelled:
        await safe_edit_status(query, "⏹ Proses dihentikan. File sementara sedang dibersihkan.")
    except asyncio.CancelledError:
        await safe_edit_status(query, "⏹ Proses dihentikan. File sementara sedang dibersihkan.")
        raise
    except Exception as error:
        LOG.exception("episode delivery failed")
        await edit_status(query, f"⚠️ Gagal: {error}")
    finally:
        if path:
            await asyncio.to_thread(remove_job_file, path)
        if jobs.get(key) is active_job:
            jobs.pop(key, None)


async def post_init(application: Application) -> None:
    settings: TelegramSettings = application.bot_data["settings"]
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    removed = await asyncio.to_thread(purge_work_dir, settings.work_dir, settings.job_ttl_seconds)
    if removed:
        LOG.info("removed %d abandoned Telegram job(s)", removed)
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Tampilkan petunjuk bot"),
            BotCommand("stop", "Hentikan download atau upload aktif"),
        ]
    )
    if application.job_queue is None:
        raise RuntimeError("Telegram job queue tidak tersedia. Install dependensi dari requirements.txt.")
    application.job_queue.run_repeating(
        _periodic_job_cleanup,
        interval=30 * 60,
        first=30 * 60,
        name="telegram-job-cleanup",
    )


async def _periodic_job_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prevent abandoned download folders from surviving a long-running bot."""
    settings: TelegramSettings = context.application.bot_data["settings"]
    removed = await asyncio.to_thread(purge_work_dir, settings.work_dir, settings.job_ttl_seconds)
    if removed:
        LOG.info("periodic cleanup removed %d Telegram job(s)", removed)


def run_bot(*, full_only: bool = False) -> None:
    settings = TelegramSettings.from_env("TELEGRAM_FULL_BOT_TOKEN" if full_only else "TELEGRAM_BOT_TOKEN")
    # Keep uploads independent from long polling. HTTP/1.1 is intentional here:
    # it is the most reliable protocol for large multipart uploads through VPS
    # networks and Telegram's Bot API edge.
    api_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=600,
        write_timeout=600,
        media_write_timeout=600,
        pool_timeout=30,
        http_version="1.1",
    )
    updates_request = HTTPXRequest(
        connection_pool_size=2,
        connect_timeout=30,
        read_timeout=60,
        write_timeout=30,
        pool_timeout=30,
        http_version="1.1",
    )
    builder = (
        Application.builder()
        .token(settings.token)
        .request(api_request)
        .get_updates_request(updates_request)
        .post_init(post_init)
    )
    if settings.api_base_url:
        builder = builder.base_url(f"{settings.api_base_url}/bot").base_file_url(
            f"{settings.api_base_url}/file/bot"
        )
    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["full_only"] = full_only
    application.bot_data["download_semaphore"] = asyncio.Semaphore(settings.max_concurrent_downloads)
    application.bot_data["active_jobs"] = {}
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CallbackQueryHandler(send_download, pattern=r"^full$" if full_only else r"^(ep:\d+|all)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    run_bot()


if __name__ == "__main__":
    main()
