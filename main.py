"""
Telegram бот — точка входу.

Команди:
  /start   — привітання
  /status  — скільки відео опубліковано сьогодні
  /queue   — що в черзі

Сценарій:
  1. Надсилаєш відео в чат
  2. Бот обробляє: silence removal → субтитри → обкладинка → S3
  3. Бот запитує: "Опублікувати зараз чи поставити в чергу?"
  4. При виборі часу — потрапляє в publish_queue
  5. queue_runner.py публікує у заданий час
"""

import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import db
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, TMP_DIR, TIKTOK_PUBLISH_TIMES, TIKTOK_DAILY_LIMIT
from pipeline.ffmpeg_processor import remove_silence, burn_subtitles, extract_frame
from pipeline.transcriber import transcribe_to_srt
from pipeline.cover_generator import generate_cover
from pipeline.uploader import upload_file
from scheduler.queue_runner import run as queue_runner_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BOT] %(message)s")
logger = logging.getLogger(__name__)


# ── Guards ────────────────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    return update.effective_user.id == TELEGRAM_ALLOWED_USER_ID


# ── Команди ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Привіт! Надішли відео — я його оброблю і поставлю в чергу для публікації.\n\n"
        "/status — статус сьогоднішніх публікацій\n"
        "/queue — черга"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    count = db.count_tiktoks_today()
    await update.message.reply_text(
        f"📊 Сьогодні опубліковано в TikTok: {count}/{TIKTOK_DAILY_LIMIT}"
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    items = db.get_pending_queue()
    if not items:
        await update.message.reply_text("Черга порожня.")
        return
    lines = [f"• {i['platform']} о {i['scheduled_at'][:16]}" for i in items]
    await update.message.reply_text("📅 Черга:\n" + "\n".join(lines))


# ── Обробка відео ─────────────────────────────────────────────────────────────

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    msg = await update.message.reply_text("⏳ Отримав відео, починаю обробку...")

    video = update.message.video or update.message.document
    if not video:
        await msg.edit_text("❌ Надішли відео файл (.mp4)")
        return

    # 1. Завантажуємо відео з Telegram
    await msg.edit_text("📥 Завантажую відео...")
    file = await context.bot.get_file(video.file_id)
    local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_raw.mp4")
    await file.download_to_drive(local_path)

    try:
        # 2. Видалення пауз
        await msg.edit_text("✂️ Видаляю паузи...")
        no_silence_path = remove_silence(local_path)

        # 3. Транскрипція → субтитри
        await msg.edit_text("📝 Транскрибую відео...")
        srt_path, transcript = transcribe_to_srt(no_silence_path)

        # 4. Burn-in субтитрів
        await msg.edit_text("🎬 Накладаю субтитри...")
        final_video_path = burn_subtitles(no_silence_path, srt_path)

        # 5. Обкладинка
        await msg.edit_text("🖼 Генерую обкладинку...")
        frame_path = extract_frame(final_video_path, timestamp=1.5)
        cover_path = generate_cover(frame_path, subtitle_text=transcript[:60] + "..." if len(transcript) > 60 else transcript)

        # 6. Завантаження на S3
        await msg.edit_text("☁️ Завантажую на S3...")
        s3_video_url = upload_file(final_video_path, prefix="videos")
        s3_cover_url = upload_file(cover_path, prefix="covers")

        # 7. Зберігаємо в БД
        video_id = db.create_video(
            original_filename=video.file_name or "video.mp4",
            s3_url=s3_video_url,
            cover_s3_url=s3_cover_url,
            transcript=transcript,
        )

        # Зберігаємо video_id в context для callback
        context.user_data["pending_video_id"] = video_id

        # 8. Питаємо коли публікувати
        keyboard = _build_schedule_keyboard()
        await msg.edit_text(
            f"✅ Відео готове!\n\n"
            f"📝 Транскрипція: _{transcript[:100]}..._\n\n"
            "Коли публікуємо в TikTok?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Помилка обробки: {e}")
    finally:
        # Прибираємо тимчасові файли
        for path in [local_path]:
            try:
                os.remove(path)
            except Exception:
                pass


def _build_schedule_keyboard() -> InlineKeyboardMarkup:
    """Кнопки з запланованими часами публікації."""
    buttons = []
    now = datetime.now()

    for time_str in TIKTOK_PUBLISH_TIMES:
        h, m = map(int, time_str.split(":"))
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)

        label = f"🕐 {time_str}"
        callback_data = f"schedule_tiktok:{scheduled.isoformat()}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback_data)])

    buttons.append([InlineKeyboardButton("🔴 Зараз", callback_data="schedule_tiktok:now")])
    return InlineKeyboardMarkup(buttons)


async def handle_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    data = query.data  # "schedule_tiktok:2026-06-11T09:00:00" або "schedule_tiktok:now"
    _, time_part = data.split(":", 1)

    video_id = context.user_data.get("pending_video_id")
    if not video_id:
        await query.edit_message_text("❌ Не знайдено відео. Надішли його знову.")
        return

    if time_part == "now":
        scheduled_at = datetime.now()
        label = "зараз"
    else:
        scheduled_at = datetime.fromisoformat(time_part)
        label = scheduled_at.strftime("%H:%M")

    db.enqueue(video_id, "tiktok", scheduled_at)
    context.user_data.pop("pending_video_id", None)

    await query.edit_message_text(
        f"✅ Поставлено в чергу на TikTok о {label}.\n"
        f"Завтра о {TIKTOK_PUBLISH_TIMES[0]} перевірю перегляди і опублікую найкраще в Instagram Reels."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()

    # Запускаємо queue_runner в окремому потоці
    queue_thread = threading.Thread(target=queue_runner_run, daemon=True)
    queue_thread.start()
    logger.info("Queue runner запущено в фоні.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(CallbackQueryHandler(handle_schedule_callback, pattern=r"^schedule_tiktok:"))

    logger.info("Бот запущено. Очікую відео...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
