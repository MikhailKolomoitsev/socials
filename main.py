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
from pipeline.ffmpeg_processor import remove_silence, normalize_vertical, burn_subtitles, extract_frame
from pipeline.transcriber import transcribe_to_srt
from pipeline.cover_generator import generate_cover_ai as generate_cover
from pipeline.caption_generator import generate_caption
from pipeline.uploader import upload_file
from scheduler.queue_runner import run as queue_runner_run
from webapp.server import start_in_background as start_webapp
from publishers.instagram import publish_reel, adapt_caption_for_instagram, get_valid_token_and_user_id
from publishers.instagram_dm import list_dm_candidates, run_broadcast

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
        "/queue — черга\n"
        "/publish_ig — опублікувати в Instagram відео, яке вже \"вибухнуло\" в TikTok\n"
        "/dm_blast <текст> — одноразова розсилка в Instagram Direct усім, хто вже писав"
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


async def cmd_publish_ig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ручна заміна автоматичної "найкращий TikTok за вчора → Instagram".

    TikTok-відео тепер публікується вручну власником (inbox-флоу), тож
    переглядів через API не отримати поки відео не опубліковане публічно.
    Натомість власник сам каже боту, яке відео "вибухнуло".
    """
    if not is_allowed(update):
        return

    videos = db.get_recent_tiktoks_for_instagram(limit=10)
    if not videos:
        await update.message.reply_text(
            "Немає відео, закинутих у TikTok, які ще не опубліковані в Instagram."
        )
        return

    buttons = []
    for v in videos:
        caption_preview = (v.get("tiktok_caption") or "").strip().replace("\n", " ")[:30]
        published = (v.get("tiktok_published_at") or "")[:16]
        label = f"{published} — {caption_preview or 'без підпису'}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"publish_ig:{v['id']}")])

    await update.message.reply_text(
        "Яке відео опублікувати в Instagram Reels?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_publish_ig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    video_id = int(query.data.split(":", 1)[1])
    video = db.get_video_by_id(video_id)
    if not video:
        await query.edit_message_text("❌ Відео не знайдено.")
        return

    await query.edit_message_text("⏳ Публікую в Instagram Reels...")

    insta_caption = adapt_caption_for_instagram(video.get("tiktok_caption", ""))
    try:
        media_id = publish_reel(
            video_url=video["s3_url"],
            caption=insta_caption,
            cover_url=video.get("cover_s3_url"),
        )
        db.set_instagram_published(video_id, media_id, insta_caption)
        await query.edit_message_text(f"✅ Опубліковано в Instagram Reels (media_id={media_id}).")
    except Exception as e:
        logger.error(f"Помилка публікації в Instagram: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Помилка публікації в Instagram: {e}")


# ── Instagram Direct: одноразова розсилка ────────────────────────────────────

async def cmd_dm_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Одноразова розсилка в Instagram Direct усім, хто вже писав акаунту.

    /dm_blast <текст> — спочатку показує прев'ю (скільки діалогів знайдено,
    скільки з них поза 24-годинним вікном і, ймовірно, отримають помилку від
    Instagram), і просить підтвердження кнопкою. Нічого не надсилається без
    явного підтвердження.
    """
    if not is_allowed(update):
        return

    text = " ".join(context.args) if context.args else ""
    if not text.strip():
        await update.message.reply_text(
            "Використання: /dm_blast <текст повідомлення>\n\n"
            "Надішле це повідомлення всім, хто вже писав акаунту в Instagram "
            "Direct (одноразово, не автовідповідач на нові повідомлення). "
            "Спочатку покажу прев'ю — нічого не надсилається без підтвердження.\n\n"
            "⚠️ Юридично автоматизовані повідомлення мають бути позначені як такі — "
            "додай це в текст сам, напр. \"(автоматичне повідомлення)\"."
        )
        return

    await update.message.reply_text("🔎 Перевіряю, кому вже можна написати...")

    try:
        access_token, ig_user_id = get_valid_token_and_user_id()
        candidates = list_dm_candidates(access_token, ig_user_id)
    except Exception as e:
        logger.error(f"Помилка отримання списку діалогів Instagram: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Не вдалось отримати список діалогів: {e}\n\n"
            "Якщо помилка про permission — потрібно додати "
            "instagram_business_manage_messages і перепройти /auth/instagram/login."
        )
        return

    if not candidates:
        await update.message.reply_text(
            "Не знайшов жодного діалогу через Instagram API. Можливо, потрібен "
            "новий permission instagram_business_manage_messages — перепройди "
            "/auth/instagram/login, якщо ще не робив цього після оновлення."
        )
        return

    within_24h = sum(1 for c in candidates if c["within_24h"])
    context.user_data["dm_blast_text"] = text

    await update.message.reply_text(
        f"📋 Знайдено {len(candidates)} діалогів.\n"
        f"✅ {within_24h} у межах 24-годинного вікна (дійде точно).\n"
        f"⚠️ {len(candidates) - within_24h} поза вікном — Instagram, ймовірно, "
        "відхилить надсилання їм (обмеження платформи, не бот).\n\n"
        f"Текст:\n{text}\n\n"
        "Підтвердити розсилку?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Надіслати ({len(candidates)})", callback_data="dm_blast_confirm"),
            InlineKeyboardButton("❌ Скасувати", callback_data="dm_blast_cancel"),
        ]]),
    )


async def handle_dm_blast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    if query.data == "dm_blast_cancel":
        context.user_data.pop("dm_blast_text", None)
        await query.edit_message_text("Скасовано.")
        return

    text = context.user_data.pop("dm_blast_text", None)
    if not text:
        await query.edit_message_text(
            "❌ Текст розсилки не знайдено (можливо, бот перезапустився) — "
            "почни знову через /dm_blast."
        )
        return

    await query.edit_message_text(
        "📤 Надсилаю... (може зайняти кілька хвилин через паузи між повідомленнями)"
    )

    try:
        access_token, ig_user_id = get_valid_token_and_user_id()
        result = await asyncio.to_thread(run_broadcast, access_token, ig_user_id, text, False, 4.0)
    except Exception as e:
        logger.error(f"Помилка розсилки Instagram DM: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Помилка розсилки: {e}")
        return

    report = (
        "✅ Розсилку завершено.\n\n"
        f"Надіслано: {len(result['sent'])}\n"
        f"Пропущено (вже надсилали раніше): {len(result['skipped_already_sent'])}\n"
        f"Не вдалось: {len(result['failed'])}"
    )
    if result["failed"]:
        sample = "\n".join(f"• {f['igsid']}: {f['error'][:80]}" for f in result["failed"][:5])
        report += f"\n\nПриклади помилок (найчастіше — поза 24-годинним вікном):\n{sample}"

    await query.edit_message_text(report)


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

    # Усі тимчасові артефакти пайплайну — прибираємо їх ВСІ у finally, а не
    # лише сире відео. Без цього no-silence-копія, srt, фінальне відео, кадр
    # і обкладинка накопичуються на диску Railway-контейнера з кожним відео
    # і з часом можуть забити диск (ENOSPC), що проявляється дивними
    # помилками типу "Unable to open ...srt" в зовсім інших місцях пайплайну.
    no_silence_path = None
    vertical_path = None
    srt_path = None
    final_video_path = None
    frame_path = None
    cover_path = None

    try:
        # 2. Видалення пауз
        await msg.edit_text("✂️ Видаляю паузи...")
        no_silence_path = remove_silence(local_path)

        # 3. Транскрипція → субтитри (до перекодування формату, щоб Whisper
        # отримав оригінальну, ще не перестиснуту звукову доріжку)
        await msg.edit_text("📝 Транскрибую відео...")
        srt_path, transcript = transcribe_to_srt(no_silence_path)

        # 4. Приводимо до вертикального 9:16 (1080×1920) — якщо відео
        # горизонтальне/квадратне, порожні поля заповнюються розмитим фоном
        # замість чорних смуг чи обрізання кадру.
        await msg.edit_text("📐 Приводжу відео до 9:16...")
        vertical_path = normalize_vertical(no_silence_path)

        # 5. Burn-in субтитрів — тільки якщо Whisper/AssemblyAI реально щось
        # розпізнали. Якщо у відео немає мовлення (тиша, музика без слів,
        # надто коротке відео), srt вийде порожнім — і спроба напалити
        # субтитри на порожній файл лише зламає весь пайплайн (саме це
        # ховалось за старою незрозумілою помилкою ffmpeg "Unable to open
        # ...srt": libass не вміє відкрити 0-байтний файл як субтитри).
        if transcript and transcript.strip():
            await msg.edit_text("🎬 Накладаю субтитри...")
            final_video_path = burn_subtitles(vertical_path, srt_path)
        else:
            await msg.edit_text("🎬 Мовлення не розпізнано — субтитри пропускаю...")
            final_video_path = vertical_path

        # 5. Обкладинка
        await msg.edit_text("🖼 Генерую обкладинку...")
        frame_path = extract_frame(final_video_path, timestamp=1.5)
        cover_path = generate_cover(transcript, frame_path)

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

        # 8. Надсилаємо обкладинку + готовий підпис для ручного використання
        if transcript and transcript.strip():
            try:
                tiktok_caption = await asyncio.to_thread(generate_caption, transcript, "tiktok")
                # Зберігаємо підпис щоб queue_runner не генерував вдруге
                db.set_tiktok_caption_draft(video_id, tiktok_caption)
                caption_preview = f"📋 *Підпис для TikTok* (скопіюй):\n\n{tiktok_caption}"
            except Exception as e:
                logger.warning(f"Caption generation failed: {e}")
                tiktok_caption = ""
                caption_preview = "_(підпис не вдалось згенерувати)_"

            try:
                with open(cover_path, "rb") as img:
                    await update.message.reply_photo(
                        photo=img,
                        caption=caption_preview,
                        parse_mode="Markdown",
                    )
            except Exception as e:
                logger.warning(f"Не вдалось надіслати обкладинку: {e}")
                # Надсилаємо хоча б підпис текстом
                await update.message.reply_text(caption_preview, parse_mode="Markdown")

        # 9. Питаємо коли публікувати
        keyboard = _build_schedule_keyboard()
        transcript_line = (
            f"📝 Транскрипція: _{transcript[:100]}..._\n\n"
            if transcript and transcript.strip()
            else "📝 Мовлення не розпізнано (без субтитрів)\n\n"
        )
        await msg.edit_text(
            f"✅ Відео готове!\n\n"
            f"{transcript_line}"
            "Коли публікуємо в TikTok?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Помилка обробки: {e}")
    finally:
        # Прибираємо всі тимчасові файли цього відео (сире, no-silence, srt,
        # фінальне відео, кадр, обкладинку) — інакше диск контейнера
        # поступово забивається і це проявляється дивними помилками на
        # начебто непов'язаних кроках пайплайну.
        for path in [local_path, no_silence_path, vertical_path, srt_path, final_video_path, frame_path, cover_path]:
            if not path:
                continue
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
        "Відео потрапить у твої TikTok-чернетки — відкрий TikTok і натисни "
        "\"Опублікувати\". Коли побачиш, що відео вибухнуло, скористайся "
        "командою /publish_ig, щоб закинути його ще й в Instagram Reels."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()

    # Запускаємо queue_runner в окремому потоці
    queue_thread = threading.Thread(target=queue_runner_run, daemon=True)
    queue_thread.start()
    logger.info("Queue runner запущено в фоні.")

    # Запускаємо веб-сервер з /terms і /privacy (для TikTok App Review)
    start_webapp()
    logger.info("Веб-сервер (/terms, /privacy) запущено в фоні.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("publish_ig", cmd_publish_ig))
    app.add_handler(CommandHandler("dm_blast", cmd_dm_blast))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(CallbackQueryHandler(handle_schedule_callback, pattern=r"^schedule_tiktok:"))
    app.add_handler(CallbackQueryHandler(handle_publish_ig_callback, pattern=r"^publish_ig:"))
    app.add_handler(CallbackQueryHandler(handle_dm_blast_callback, pattern=r"^dm_blast_(confirm|cancel)$"))

    logger.info("Бот запущено. Очікую відео...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
