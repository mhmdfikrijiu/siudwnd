from __future__ import annotations

import asyncio
import logging
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
    episodes: list[dict]


def episode_keyboard(episodes: list[dict]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(f"Ep {item['number']:02d}", callback_data=f"ep:{item['number']}") for item in episodes]
    rows = [buttons[index : index + 4] for index in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("🎬 FULL — gabung semua episode", callback_data="full")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🎬 Narto Downloader\n\nKirim link drama dari narto-drama.com. Setelah itu pilih episode yang ingin dikirim."
        "\n\nFile hanya disimpan sementara selama proses kirim, lalu otomatis dihapus.",
    )


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    status = await update.effective_message.reply_text("🔎 Mengecek daftar episode…")
    try:
        slug, title, episodes = await asyncio.to_thread(resolve, text)
        context.user_data["drama"] = DramaSelection(slug, title, episodes)
        await status.edit_text(
            f"🎬 {title}\n\n"
            f"📚 {len(episodes)} episode tersedia\n"
            "📥 Pilih episode di bawah, atau pilih FULL untuk menggabungkan semuanya menjadi satu MP4.\n\n"
            "ℹ️ File dibersihkan otomatis setelah berhasil dikirim.",
            reply_markup=episode_keyboard(episodes),
        )
    except Exception as error:
        LOG.info("resolve failed: %s", error)
        await status.edit_text(f"⚠️ {error}")


async def send_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    selection: DramaSelection | None = context.user_data.get("drama")
    if not selection:
        await query.edit_message_text("Sesi sudah berakhir. Kirim link drama lagi.")
        return
    is_full = query.data == "full"
    episode = None if is_full else int(query.data.split(":", 1)[1])
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
        future = asyncio.run_coroutine_threadsafe(query.edit_message_text(progress_text()), event_loop)
        future.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    if is_full:
        await query.edit_message_text(progress_text())
    else:
        await query.edit_message_text(f"⏳ Mengunduh\n\n🎬 {selection.title}\n📺 Episode {episode:02d}")
    path = None
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
        if size > settings.max_upload_bytes:
            raise DownloadError(f"File {size / 1024 / 1024:.1f} MB melebihi batas bot ({settings.max_upload_bytes / 1024 / 1024:.0f} MB).")
        if is_full:
            await query.edit_message_text(
                f"📤 Mengunggah FULL ke Telegram\n\n🎬 {selection.title}\n"
                f"📦 {size / 1024 / 1024:.1f} MB\n⏳ Jangan tekan tombol lagi sampai file muncul."
            )
        with path.open("rb") as file_handle:
            await query.message.reply_document(
                document=file_handle,
                filename=path.name,
                caption=(f"✅ {selection.title}\nFULL · {len(selection.episodes)} episode · {size / 1024 / 1024:.1f} MB"
                         if is_full else f"✅ {selection.title}\nEpisode {episode:02d} · {size / 1024 / 1024:.1f} MB"),
            )
        await query.edit_message_text("✅ File terkirim dan salinan sementara di server sudah dibersihkan.")
    except TimedOut:
        # Telegram may accept and deliver a large upload before its API response
        # reaches us. Treat this as an uncertain delivery, never as a hard failure.
        LOG.warning("Telegram timed out after upload for %s", path.name if path else "unknown file")
        await query.edit_message_text(
            "⚠️ Respons Telegram terlambat. Jika file sudah muncul di chat, pengiriman berhasil—jangan unduh ulang. "
            "Salinan sementara di server tetap dibersihkan."
        )
    except Exception as error:
        LOG.exception("episode delivery failed")
        await query.edit_message_text(f"⚠️ Gagal: {error}")
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


def main() -> None:
    settings = TelegramSettings.from_env()
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
    application.bot_data["download_semaphore"] = asyncio.Semaphore(settings.max_concurrent_downloads)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(send_download, pattern=r"^(ep:\d+|full)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
