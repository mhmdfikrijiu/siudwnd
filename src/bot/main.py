from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from src.core.config import TelegramSettings
from src.services.telegram_downloads import DownloadError, download_full, download_one, purge_work_dir, remove_job_file, resolve

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
            "Episode akan dikirim satu per satu segera setelah selesai diunduh."
        )
        sent: list[int] = []
        failed: list[int] = []
        try:
            async with semaphore:
                for index, item in enumerate(selection.episodes, start=1):
                    number = item["number"]
                    path = None
                    try:
                        await edit_status(query,
                            f"📥 Mengunduh Episode {number:02d}\n\n🎬 {selection.title}\n"
                            f"📤 Terkirim: {len(sent)}/{len(selection.episodes)}\n"
                            f"⏳ Antrean: episode {index}/{len(selection.episodes)}"
                        )
                        path = await asyncio.to_thread(download_one, selection.slug, number, settings.work_dir)
                        size = path.stat().st_size
                        if size > settings.max_upload_bytes:
                            raise DownloadError(
                                f"Ep {number:02d} ({size / 1024 / 1024:.1f} MB) melewati batas upload bot."
                            )
                        await edit_status(query,
                            f"📤 Mengirim Episode {number:02d}\n\n🎬 {selection.title}\n"
                            f"📦 {size / 1024 / 1024:.1f} MB"
                        )
                        with path.open("rb") as file_handle:
                            await query.message.reply_document(
                                document=file_handle,
                                filename=path.name,
                                caption=f"✅ {selection.title}\nEpisode {number:02d} · {size / 1024 / 1024:.1f} MB",
                            )
                        sent.append(number)
                    except TimedOut:
                        # Do not resend automatically: Telegram may already have the file.
                        sent.append(number)
                        LOG.warning("Telegram timed out while sending episode %s", number)
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
        except Exception as error:
            LOG.exception("send-all failed")
            await edit_status(query, f"⚠️ Kirim semua berhenti: {error}")
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
                    settings.max_concurrent_downloads, on_full_progress,
                )
            else:
                path = await asyncio.to_thread(download_one, selection.slug, episode, settings.work_dir)
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
    except Exception as error:
        LOG.exception("episode delivery failed")
        await edit_status(query, f"⚠️ Gagal: {error}")
    finally:
        if path:
            await asyncio.to_thread(remove_job_file, path)


async def post_init(application: Application) -> None:
    settings: TelegramSettings = application.bot_data["settings"]
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    removed = await asyncio.to_thread(purge_work_dir, settings.work_dir, settings.job_ttl_seconds)
    if removed:
        LOG.info("removed %d abandoned Telegram job(s)", removed)
    application.create_task(_periodic_job_cleanup(application), name="telegram-job-cleanup")


async def _periodic_job_cleanup(application: Application) -> None:
    """Prevent abandoned download folders from surviving a long-running bot."""
    settings: TelegramSettings = application.bot_data["settings"]
    while True:
        await asyncio.sleep(30 * 60)
        removed = await asyncio.to_thread(purge_work_dir, settings.work_dir, settings.job_ttl_seconds)
        if removed:
            LOG.info("periodic cleanup removed %d Telegram job(s)", removed)


def run_bot(*, full_only: bool = False) -> None:
    settings = TelegramSettings.from_env("TELEGRAM_FULL_BOT_TOKEN" if full_only else "TELEGRAM_BOT_TOKEN")
    builder = (
        Application.builder()
        .token(settings.token)
        .connect_timeout(30)
        .write_timeout(600)
        .read_timeout(600)
        .pool_timeout(30)
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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(send_download, pattern=r"^full$" if full_only else r"^(ep:\d+|all)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    run_bot()


if __name__ == "__main__":
    main()
