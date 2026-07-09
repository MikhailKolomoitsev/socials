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
import html
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta

import requests as http_requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest as TgBadRequest
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
from pipeline.ffmpeg_processor import to_standard_mp4, remove_silence, normalize_vertical, burn_subtitles, extract_frame
from pipeline.transcriber import transcribe_to_srt
from pipeline.cover_generator import generate_cover_ai as generate_cover
from pipeline.caption_generator import generate_caption
from pipeline.uploader import upload_file
from scheduler.queue_runner import run as queue_runner_run
from webapp.server import start_in_background as start_webapp
from publishers.instagram import publish_reel, adapt_caption_for_instagram, get_valid_token_and_user_id
from publishers.instagram_dm import list_dm_candidates, run_broadcast
from pipeline.drive_watcher import list_all_videos, download_file as drive_download, is_processing, mark_processing, unmark_processing, extract_file_id
from config import GOOGLE_DRIVE_FOLDER_ID

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
        "Якщо відео >20MB — надішли посилання:\n"
        "`/process_url https://...`\n\n"
        "/status — статус сьогоднішніх публікацій\n"
        "/queue — черга\n"
        "/publish_ig — опублікувати в Instagram відео, яке вже \"вибухнуло\" в TikTok\n"
        "/dm_blast <текст> — одноразова розсилка в Instagram Direct усім, хто вже писав",
        parse_mode="Markdown",
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

async def cmd_process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /process_url <https://...>  — завантажити відео за посиланням.

    Підтримує:
      • Google Drive посилання (drive.google.com/file/d/...) — через Service Account
      • Будь-яке пряме посилання на mp4 (Dropbox ?dl=1, власний S3 тощо)
    """
    if not is_allowed(update):
        return

    url = " ".join(context.args).strip() if context.args else ""
    if not url.startswith("http"):
        await update.message.reply_text(
            "Використання: `/process_url https://...`\n\n"
            "Підтримується Google Drive та будь-яке пряме посилання на mp4.",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text("📥 Завантажую відео...")

    try:
        # Google Drive — завантажуємо через Service Account (без ліміту розміру)
        if "drive.google.com" in url or "docs.google.com" in url:
            file_id = extract_file_id(url)
            if not file_id:
                await msg.edit_text("❌ Не вдалось витягти ID файлу з посилання Google Drive.")
                return
            local_path = await asyncio.to_thread(drive_download, file_id, "video.mp4")
        else:
            # Пряме посилання
            local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_raw.mp4")
            resp = await asyncio.to_thread(
                lambda: http_requests.get(url, stream=True, timeout=120)
            )
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось завантажити відео: {e}")
        return

    await _process_video_file(update, context, msg, local_path)


async def cmd_scan_drive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /scan_drive — показує нові відео у папці Google Drive.
    Кожне відео — кнопка, натискаєш → бот завантажує і обробляє.
    """
    if not is_allowed(update):
        return

    if not GOOGLE_DRIVE_FOLDER_ID:
        await update.message.reply_text(
            "❌ GOOGLE_DRIVE_FOLDER_ID не задано.\n"
            "Додай ID папки в Railway Variables і перезапусти бот."
        )
        return

    await update.message.reply_text("🔍 Перевіряю папку Google Drive...")

    try:
        files = await asyncio.to_thread(list_new_videos)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка доступу до Drive: {e}")
        return

    if not files:
        await update.message.reply_text(
            "✅ Нових відео в папці немає.\n\n"
            "Закинь відео у папку Google Drive і натисни /scan_drive знову."
        )
        return

    buttons = []
    for f in files[:10]:  # максимум 10 кнопок
        size_mb = int(f.get("size", 0)) // (1024 * 1024)
        label = f"🎬 {f['name']} ({size_mb} MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"drive_process:{f['id']}:{f['name']}")])

    await update.message.reply_text(
        f"📂 Знайдено {len(files)} нових відео у Google Drive.\nОбери яке обробити:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_drive_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє натискання кнопки для конкретного файлу з Drive."""
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    _, file_id, filename = query.data.split(":", 2)
    msg = await query.edit_message_text(f"📥 Завантажую «{filename}» з Google Drive...")

    try:
        local_path = await asyncio.to_thread(drive_download, file_id, filename)
        mark_processed(file_id)
    except Exception as e:
        await msg.edit_text(f"❌ Помилка завантаження з Drive: {e}")
        return

    await _process_video_file(update, context, msg, local_path, filename)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    msg = await update.message.reply_text("⏳ Отримав відео, починаю обробку...")

    video = update.message.video or update.message.document
    if not video:
        await msg.edit_text("❌ Надішли відео файл (.mp4)")
        return

    # Telegram Bot API дозволяє завантажувати файли лише до ~20MB.
    await msg.edit_text("📥 Завантажую відео...")
    local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_raw.mp4")
    try:
        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(local_path)
    except TgBadRequest as e:
        if "file is too big" in str(e).lower():
            await msg.edit_text(
                "❌ Відео завелике для Telegram Bot API (ліміт ~20MB).\n\n"
                "Надішли пряме посилання на відео:\n"
                "`/process_url https://example.com/video.mp4`\n\n"
                "Або стисни відео перед відправкою.",
                parse_mode="Markdown",
            )
        else:
            await msg.edit_text(f"❌ Помилка Telegram: {e}")
        return

    original_name = getattr(video, "file_name", None) or "video.mp4"
    await _process_video_file(update, context, msg, local_path, original_name)


async def _process_video_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    msg,
    local_path: str,
    original_name: str = "video.mp4",
):
    """Спільний пайплайн обробки відео для handle_video і cmd_process_url."""
    std_path = None
    no_silence_path = None
    vertical_path = None
    srt_path = None
    final_video_path = None
    frame_path = None
    cover_path = None

    try:
        # 0. Конвертуємо до стандартного H.264/AAC (MOV, HEVC, VFR → mp4 30fps)
        await msg.edit_text("🔄 Конвертую формат відео...")
        std_path = await asyncio.to_thread(to_standard_mp4, local_path)

        # 1. Видалення пауз
        await msg.edit_text("✂️ Видаляю паузи...")
        no_silence_path = remove_silence(std_path)

        # 2. Транскрипція → субтитри
        await msg.edit_text("📝 Транскрибую відео...")
        srt_path, transcript = transcribe_to_srt(no_silence_path)

        # 3. Нормалізація до 9:16
        await msg.edit_text("📐 Приводжу відео до 9:16...")
        vertical_path = normalize_vertical(no_silence_path)

        # 4. Burn-in субтитрів
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
            original_filename=original_name,
            s3_url=s3_video_url,
            cover_s3_url=s3_cover_url,
            transcript=transcript,
        )
        context.user_data["pending_video_id"] = video_id

        # 8. Надсилаємо обкладинку + підпис окремим повідомленням (code-блок = кнопка Copy)
        if transcript and transcript.strip():
            try:
                tiktok_caption = await asyncio.to_thread(generate_caption, transcript, "tiktok")
                db.set_tiktok_caption_draft(video_id, tiktok_caption)
            except Exception as e:
                logger.warning(f"Caption generation failed: {e}")
                tiktok_caption = None

            # Обкладинка окремим фото
            try:
                with open(cover_path, "rb") as img:
                    await update.message.reply_photo(photo=img, caption="🖼 Обкладинка")
            except Exception as e:
                logger.warning(f"Не вдалось надіслати обкладинку: {e}")

            # Підпис окремим повідомленням у code-блоку → Telegram показує кнопку Copy
            if tiktok_caption:
                await update.message.reply_text(
                    f"📋 Підпис для TikTok — натисни щоб скопіювати:\n\n<pre>{html.escape(tiktok_caption)}</pre>",
                    parse_mode="HTML",
                )

        # 9. Питаємо коли публікувати
        keyboard = _build_schedule_keyboard()
        transcript_line = (
            f"📝 Транскрипція: _{transcript[:100]}..._\n\n"
            if transcript and transcript.strip()
            else "📝 Мовлення не розпізнано (без субтитрів)\n\n"
        )
        await msg.edit_text(
            f"✅ Відео готове!\n\n{transcript_line}Коли публікуємо в TikTok?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Помилка обробки: {e}")
    finally:
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


# ── Google Drive auto-poller ──────────────────────────────────────────────────

DRIVE_POLL_INTERVAL = 120  # секунди між перевірками папки


async def _drive_poll_job(context: ContextTypes.DEFAULT_TYPE):
    """
    PTB job: запускається кожні DRIVE_POLL_INTERVAL секунд.
    Перевіряє папку Drive на нові відео і одразу обробляє їх.
    """
    if not GOOGLE_DRIVE_FOLDER_ID:
        return

    chat_id = TELEGRAM_ALLOWED_USER_ID
    try:
        files = await asyncio.to_thread(list_all_videos)
    except Exception as e:
        logger.error(f"Drive poller помилка: {e}", exc_info=True)
        return

    for f in files:
        file_id = f["id"]
        filename = f["name"]
        size_mb = int(f.get("size", 0)) // (1024 * 1024)

        # 1. Вже в обробці прямо зараз (паралельний запуск poll)
        if is_processing(file_id):
            continue

        # 2. Вже є в БД по назві файлу — оброблялось раніше (навіть після рестарту)
        if db.is_filename_known(filename):
            logger.debug(f"Drive poller: «{filename}» вже в БД — пропускаємо")
            continue

        logger.info(f"Drive poller: нове відео «{filename}» ({size_mb} MB)")
        mark_processing(file_id)

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"📂 Знайшов нове відео у Google Drive:\n<code>{html.escape(filename)}</code> ({size_mb} MB)\n\n⏳ Завантажую і починаю обробку...",
            parse_mode="HTML",
        )

        try:
            local_path = await asyncio.to_thread(drive_download, file_id, filename)
        except Exception as e:
            unmark_processing(file_id)
            await msg.edit_text(f"❌ Не вдалось завантажити «{filename}» з Drive: {e}")
            continue

        await _process_drive_file(context.application, chat_id, msg, local_path, filename, file_id)


async def _process_drive_file(app, chat_id: int, msg, local_path: str, filename: str, file_id: str = ""):
    """
    Запускає пайплайн обробки для файлу з Drive без реального telegram.Update.
    Надсилає результати напряму в chat_id.
    """
    std_path = no_silence_path = vertical_path = srt_path = None
    final_video_path = frame_path = cover_path = None

    try:
        await msg.edit_text(f"🔄 «{filename}» — конвертую формат...")
        std_path = await asyncio.to_thread(to_standard_mp4, local_path)

        await msg.edit_text(f"✂️ «{filename}» — видаляю паузи...")
        no_silence_path = await asyncio.to_thread(remove_silence, std_path)

        await msg.edit_text(f"📝 «{filename}» — транскрибую...")
        srt_path, transcript = await asyncio.to_thread(transcribe_to_srt, no_silence_path)

        await msg.edit_text(f"📐 «{filename}» — приводжу до 9:16...")
        vertical_path = await asyncio.to_thread(normalize_vertical, no_silence_path)

        if transcript and transcript.strip():
            await msg.edit_text(f"🎬 «{filename}» — накладаю субтитри...")
            final_video_path = await asyncio.to_thread(burn_subtitles, vertical_path, srt_path)
        else:
            final_video_path = vertical_path

        await msg.edit_text(f"🖼 «{filename}» — генерую обкладинку...")
        frame_path = await asyncio.to_thread(extract_frame, final_video_path, 1.5)
        cover_path = await asyncio.to_thread(generate_cover, transcript, frame_path)

        await msg.edit_text(f"☁️ «{filename}» — завантажую на S3...")
        s3_video_url = await asyncio.to_thread(upload_file, final_video_path, "videos")
        s3_cover_url = await asyncio.to_thread(upload_file, cover_path, "covers")

        video_id = db.create_video(
            original_filename=filename,
            s3_url=s3_video_url,
            cover_s3_url=s3_cover_url,
            transcript=transcript,
        )

        # Підпис + обкладинка
        if transcript and transcript.strip():
            try:
                tiktok_caption = await asyncio.to_thread(generate_caption, transcript, "tiktok")
                db.set_tiktok_caption_draft(video_id, tiktok_caption)
            except Exception as e:
                logger.warning(f"Caption generation failed: {e}")
                tiktok_caption = None

            try:
                with open(cover_path, "rb") as img:
                    await app.bot.send_photo(chat_id=chat_id, photo=img, caption="🖼 Обкладинка")
            except Exception as e:
                logger.warning(f"Не вдалось надіслати обкладинку: {e}")

            if tiktok_caption:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"📋 Підпис для TikTok — натисни щоб скопіювати:\n\n<pre>{html.escape(tiktok_caption)}</pre>",
                    parse_mode="HTML",
                )

        # Клавіатура планування — зберігаємо video_id через inline дані
        keyboard = _build_drive_schedule_keyboard(video_id)
        transcript_line = f"📝 _{transcript[:100]}..._\n\n" if transcript and transcript.strip() else ""
        await msg.edit_text(
            f"✅ «{filename}» готове!\n\n{transcript_line}Коли публікуємо в TikTok?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Drive pipeline error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Помилка обробки «{filename}»: {e}")
    finally:
        unmark_processing(file_id)
        for path in [local_path, std_path, no_silence_path, vertical_path, srt_path, final_video_path, frame_path, cover_path]:
            if not path:
                continue
            try:
                os.remove(path)
            except Exception:
                pass


def _build_drive_schedule_keyboard(video_id: int) -> InlineKeyboardMarkup:
    """Кнопки планування для відео з Drive (video_id вбудований у callback_data)."""
    buttons = []
    now = datetime.now()
    for time_str in TIKTOK_PUBLISH_TIMES:
        h, m = map(int, time_str.split(":"))
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        buttons.append([InlineKeyboardButton(
            f"🕐 {time_str}",
            callback_data=f"schedule_drive:{video_id}:{scheduled.isoformat()}",
        )])
    buttons.append([InlineKeyboardButton(
        "🔴 Зараз",
        callback_data=f"schedule_drive:{video_id}:now",
    )])
    return InlineKeyboardMarkup(buttons)


async def handle_drive_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє вибір часу публікації для відео що прийшло з Drive."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return

    _, video_id_str, time_part = query.data.split(":", 2)
    video_id = int(video_id_str)

    if time_part == "now":
        scheduled_at = datetime.now()
        label = "зараз"
    else:
        scheduled_at = datetime.fromisoformat(time_part)
        label = scheduled_at.strftime("%H:%M")

    db.enqueue(video_id, "tiktok", scheduled_at)
    await query.edit_message_text(
        f"✅ Поставлено в чергу на TikTok о {label}.\n"
        "Відео потрапить у твої TikTok-чернетки — відкрий TikTok і натисни «Опублікувати»."
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
    app.add_handler(CommandHandler("process_url", cmd_process_url))
    app.add_handler(CommandHandler("scan_drive", cmd_scan_drive))
    app.add_handler(CallbackQueryHandler(handle_drive_process_callback, pattern=r"^drive_process:"))
    app.add_handler(CallbackQueryHandler(handle_drive_schedule_callback, pattern=r"^schedule_drive:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(CallbackQueryHandler(handle_schedule_callback, pattern=r"^schedule_tiktok:"))
    app.add_handler(CallbackQueryHandler(handle_publish_ig_callback, pattern=r"^publish_ig:"))
    app.add_handler(CallbackQueryHandler(handle_dm_blast_callback, pattern=r"^dm_blast_(confirm|cancel)$"))

    # Drive poller: перевіряє папку кожні 2 хвилини
    if GOOGLE_DRIVE_FOLDER_ID:
        app.job_queue.run_repeating(
            _drive_poll_job,
            interval=DRIVE_POLL_INTERVAL,
            first=30,  # перша перевірка через 30с після старту
            name="drive_poller",
        )
        logger.info(f"Drive poller: перевірка кожні {DRIVE_POLL_INTERVAL}с.")

    logger.info("Бот запущено. Очікую відео...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
