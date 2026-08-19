"""
Telegram бот — точка входу.

Постійне меню кнопок (під полем вводу, з'являється після /start), 2 колонки:
  📋 Неопубліковані тіктоки  /tiktok_pending  — ще НЕ відправлені в TikTok
  ✅ Відправлені тіктоки     /tiktok_sent     — вже в TikTok, є "відправити повторно"
  🎬 Неопубліковані Reels    /ig_pending      — TikTok-відео ще не в Instagram
  📸 Опубліковані Reels      /ig_sent         — вже в Instagram, є "відправити повторно"
  📊 Статистика              /stats           — перегляди/лайки/коменти + рекомендації
  ⚠️ Невдалі відео           /failed_videos   — обробка впала, є "спробувати ще раз"
Кожен список пагінований (кнопка "▶️ Показати ще", якщо є більше).

Команди:
  /start          — привітання, показує меню кнопок
  /status         — скільки відео опубліковано в TikTok сьогодні (з ліміту TIKTOK_DAILY_LIMIT)
  /queue          — заплановані Instagram-каруселі (TikTok/Reels — повністю ручні, у черзі їх немає)
  /publish_ig     — те саме, що /ig_pending, одним списком з реальними переглядами TikTok
  /nocap          — опублікувати карусель без підпису (пропустити крок підпису)

Сценарій відео:
  1. Надсилаєш відео в чат
  2. Бот обробляє: silence removal → субтитри → обкладинка → S3
  3. Відео НЕ відправляється в TikTok автоматично — повідомлення про
     готовність містить кнопку "📤 Відправити в TikTok" (те саме доступно
     пізніше зі списку "📋 Неопубліковані тіктоки"); власник сам вирішує,
     коли і яке відео відправити. Той самий publish_tt-хендлер повторно
     використовується і для "🔁 Відправити повторно" з "✅ Відправлені тіктоки"
  4. Тап кнопки одразу викликає TikTok API (у TikTok-чернетки — публічний
     автопостинг без App Review неможливий) і надсилає в чат нагадування
     "опублікуй" з кнопкою для Instagram Reels
  5. Instagram публікується ЛИШЕ вручну — тапом кнопки "Запостити в
     Instagram" (з нагадування, /ig_pending, /publish_ig, /stats або
     повторно з "📸 Опубліковані Reels"), ніколи автоматично
  6. Щодня о 13:00 за Києвом (18:00 за Балі) — нагадування, ЯКЩО сьогодні
     опубліковано менше TIKTOK_DAILY_LIMIT тіктоків
  7. Раз на ~3 дні (якщо є що показати) — нагадування обрати TikTok-відео
     для Instagram Reels і/або довести до кінця незаплановану карусель
  8. Якщо пайплайн обробки впав — відео потрапляє у "⚠️ Невдалі відео" з
     кнопкою "Спробувати ще раз" (повторне завантаження з джерела: Telegram
     file_id / Google Drive file_id / URL — і той самий пайплайн заново)
  9. YouTube Shorts — ЄДИНА платформа з ПОВНІСТЮ автоматичною публічною
     публікацією (жодної кнопки): одразу після обробки, ще до видалення
     тимчасових файлів (publishers/youtube.py — YouTube Data API не вміє
     "pull from URL", тільки прямий upload локального файлу). Працює лише
     після /auth/youtube/login; якщо OAuth не пройдено — крок мовчки
     пропускається, решта пайплайну не ламається

Сценарій сторітейл-каруселі (Instagram):
  1. Надсилаєш кілька готових слайдів ОДНИМ альбомом фото (2-10 штук)
  2. Бот вивантажує їх на S3, питає підпис (текстом або /nocap)
  3. Обираєш час публікації — queue_runner.py публікує каруселлю автоматично
     (єдине, що досі публікується за розкладом, а не кнопкою — свідомо: вибір
     часу на цьому кроці і Є підтвердженням)
"""

import asyncio
import html
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import requests as http_requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup
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
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, TMP_DIR,
    TIKTOK_PUBLISH_TIMES, TIKTOK_DAILY_LIMIT, INSTAGRAM_PUBLISH_HOUR,
    YOUTUBE_CLIENT_ID,
)
from pipeline.ffmpeg_processor import to_standard_mp4, remove_silence, normalize_vertical, burn_subtitles, extract_frame
from pipeline.transcriber import transcribe_words, build_ass_for_style, save_ass
from pipeline.cover_generator import generate_cover_ai as generate_cover
from pipeline.caption_generator import generate_caption
from pipeline.uploader import upload_file
from scheduler.queue_runner import run as queue_runner_run
from webapp.server import start_in_background as start_webapp
from publishers.instagram import publish_reel, adapt_caption_for_instagram, get_valid_token_and_user_id, test_connection as ig_test_connection
from publishers.instagram_dm import list_dm_candidates, run_broadcast
from publishers.tiktok import list_recent_public_videos as tiktok_list_recent_public_videos, publish_video as tiktok_publish_video
from publishers.youtube import publish_short as youtube_publish_short
from pipeline.drive_watcher import list_all_videos, download_file as drive_download, is_processing, mark_processing, unmark_processing, extract_file_id
from config import GOOGLE_DRIVE_FOLDER_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BOT] %(message)s")
logger = logging.getLogger(__name__)


# ── Guards ────────────────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    return update.effective_user.id == TELEGRAM_ALLOWED_USER_ID


# ── Постійне меню кнопок ─────────────────────────────────────────────────────
#
# Замість того, щоб пам'ятати слеш-команди напам'ять — кнопки завжди на
# екрані під полем вводу. Натискання надсилає звичайне текстове повідомлення
# з міткою кнопки, тому це ловиться MessageHandler(filters.Text([...]))
# (main(), зареєстрований ПЕРЕД вільним текстовим хендлером каруселі).

BTN_TIKTOK_PENDING = "📋 Неопубліковані тіктоки"
BTN_TIKTOK_SENT = "✅ Відправлені тіктоки"
BTN_IG_PENDING = "🎬 Неопубліковані Reels"
BTN_IG_SENT = "📸 Опубліковані Reels"
BTN_STATS = "📊 Статистика"
BTN_FAILED = "⚠️ Невдалі відео"


# Розмір сторінки для пагінованих списків (📋/✅/📸/📊/⚠️) — кнопка
# "▶️ Показати ще" з'являється, лише коли сторінка вийшла повною (евристика:
# рівно PAGE_SIZE елементів означає, що далі, ймовірно, є ще).
PAGE_SIZE = 15


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_TIKTOK_PENDING, BTN_TIKTOK_SENT],
            [BTN_IG_PENDING, BTN_IG_SENT],
            [BTN_STATS, BTN_FAILED],
        ],
        resize_keyboard=True,
    )


# ── Команди ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Привіт! Надішли відео — я його оброблю (паузи, субтитри, обкладинка) "
        "і одразу дам кнопку «📤 Відправити в TikTok» — тисни, коли сам вирішиш. "
        "Автоматично в TikTok нічого НЕ йде. Після тапу відео завантажиться "
        "в TikTok-чернетки, і я надішлю нагадування відкрити застосунок і "
        "натиснути «Опублікувати» (TikTok просить це зробити вручну — обмеження "
        "платформи, автопостинг без App Review неможливий) + кнопку, щоб одразу "
        "закинути це саме відео в Instagram Reels.\n\n"
        "Якщо відео >20MB — надішли посилання:\n"
        "`/process_url https://...`\n\n"
        "Надішли кілька фото одним альбомом — запропоную опублікувати їх як "
        "сторітейл-карусель в Instagram.\n\n"
        "/status — статус сьогоднішніх публікацій в TikTok\n"
        "/queue — заплановані Instagram-каруселі\n"
        "/test_ig — безпечна перевірка, чи працює публікація в Instagram (нічого не публікує)\n"
        "/dm_blast <текст> — одноразова розсилка в Instagram Direct усім, хто вже писав\n\n"
        f"Кнопки внизу:\n"
        f"{BTN_TIKTOK_PENDING} — оброблені відео, ще не відправлені в TikTok, з кнопкою на кожне\n"
        f"{BTN_TIKTOK_SENT} — відео, вже відправлені в TikTok, з темою кожного і кнопкою «Відправити повторно»\n"
        f"{BTN_IG_PENDING} — TikTok-відео, ще не опубліковані в Instagram, з переходом до відео в чаті\n"
        f"{BTN_IG_SENT} — відео, вже опубліковані в Instagram, з кнопкою «Відправити повторно»\n"
        f"{BTN_STATS} — перегляди/лайки/коменти TikTok і що варто запостити в Instagram\n"
        f"{BTN_FAILED} — відео, де обробка впала, з кнопкою «Спробувати ще раз»\n\n"
        "Раз на ~3 дні також нагадаю обрати відео для Instagram Reels і/або "
        "доробити незаплановану карусель, якщо є що.",
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    count = db.count_tiktoks_today()
    await update.message.reply_text(
        f"📊 Сьогодні опубліковано в TikTok: {count}/{TIKTOK_DAILY_LIMIT}"
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /queue — тепер показує ЛИШЕ заплановані Instagram-каруселі: TikTok і
    Instagram Reels повністю ручні (кнопки, не publish_queue), тож
    publish_queue у поточній логіці отримує записи виключно з
    platform='instagram_carousel' (enqueue_carousel — єдине місце виклику
    db.enqueue-подібної функції, що лишилось).
    """
    if not is_allowed(update):
        return
    items = db.get_pending_queue()
    if not items:
        await update.message.reply_text(
            "Запланованих Instagram-каруселей немає.\n\n"
            "(TikTok і Instagram Reels публікуються кнопками, не за розкладом — "
            "дивись 📋/✅/🎬/📸 в меню.)"
        )
        return
    lines = [f"• карусель #{i['carousel_id']} о {i['scheduled_at'][:16]} (UTC)" for i in items]
    await update.message.reply_text("📅 Заплановані Instagram-каруселі:\n" + "\n".join(lines))


async def cmd_publish_ig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ручна заміна автоматичної "найкращий TikTok за вчора → Instagram".

    TikTok-відео тепер публікується вручну власником (inbox-флоу), тож
    переглядів через API не отримати поки відео не опубліковане публічно.
    Тому тут: підтягуємо список ВЖЕ публічних TikTok-відео (video.list,
    реальні перегляди) і зіставляємо їх з чернетками за найближчим часом —
    але вибір, яке саме публікувати в Instagram, завжди лишається за власником.
    """
    if not is_allowed(update):
        return

    videos = db.get_recent_tiktoks_for_instagram(limit=10)
    if not videos:
        await update.message.reply_text(
            "Немає відео, закинутих у TikTok, які ще не опубліковані в Instagram."
        )
        return

    msg = await update.message.reply_text("🔎 Перевіряю реальні перегляди в TikTok...")

    videos = await asyncio.to_thread(_attach_tiktok_views, videos)
    # Найбільше переглядів — першими; ще не зіставлені (views=None) — в кінці.
    videos.sort(key=lambda v: (v.get("tiktok_public_views") is None, -(v.get("tiktok_public_views") or 0)))

    buttons = []
    for v in videos:
        caption_preview = (v.get("tiktok_caption") or "").strip().replace("\n", " ")[:26]
        published = (v.get("tiktok_published_at") or "")[:16]
        views = v.get("tiktok_public_views")
        views_label = f"👁{_format_views(views)}" if views is not None else "❔ не публічне"
        label = f"{views_label} · {published} · {caption_preview or 'без підпису'}"
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"publish_ig:{v['id']}")])

    await msg.edit_text(
        "Яке відео опублікувати в Instagram Reels?\n"
        "👁 — реальні перегляди TikTok (якщо відео вже опубліковане вручну). "
        "❔ — ще в чернетках або не вдалось зіставити, публікуй на власний розсуд.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _attach_tiktok_views(videos: list) -> list:
    """Синхронна функція (виконується в потоці через asyncio.to_thread):
    підтягує список публічних TikTok-відео і зіставляє з переданими
    кандидатами за найближчим часом публікації. Зіставлення зберігається
    в БД (match_tiktok_public_video), щоб не робити його повторно щоразу."""
    try:
        public_videos = tiktok_list_recent_public_videos(max_count=20)
    except Exception as e:
        logger.warning(f"Не вдалось отримати video.list з TikTok: {e}")
        return videos

    used_public_ids = {v["tiktok_public_video_id"] for v in videos if v.get("tiktok_public_video_id")}

    for cand in videos:
        if cand.get("tiktok_public_video_id"):
            continue  # вже зіставлено раніше — не перезаписуємо

        try:
            draft_time = datetime.fromisoformat(cand["tiktok_published_at"])
        except (TypeError, ValueError):
            continue

        best, best_diff = None, None
        for pv in public_videos:
            if pv["id"] in used_public_ids:
                continue
            # Публічна публікація завжди відбувається ПІСЛЯ того, як відео
            # потрапило в чернетки (± кілька хвилин на похибку годинників).
            if pv["create_time"] < draft_time - timedelta(minutes=5):
                continue
            diff = (pv["create_time"] - draft_time).total_seconds()
            if best is None or diff < best_diff:
                best, best_diff = pv, diff

        # Приймаємо збіг лише в межах 7 днів — інакше це, ймовірно, зовсім
        # інше відео (власник міг публікувати в іншому порядку/з затримкою).
        if best and best_diff is not None and best_diff <= 7 * 86400:
            used_public_ids.add(best["id"])
            views = best.get("view_count", 0)
            likes = best.get("like_count", 0)
            comments = best.get("comment_count", 0)
            share_url = best.get("share_url", "")
            db.match_tiktok_public_video(cand["id"], best["id"], views, share_url, likes, comments)
            cand["tiktok_public_video_id"] = best["id"]
            cand["tiktok_public_views"] = views
            cand["tiktok_public_likes"] = likes
            cand["tiktok_public_comments"] = comments
            cand["tiktok_public_share_url"] = share_url

    return videos


def _format_views(n: int) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


async def cmd_ig_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ig_pending — список TikTok-відео, які ще НЕ опубліковані в Instagram.

    На відміну від /publish_ig (одне повідомлення зі списком кнопок), тут
    кожне відео надсилається ОКРЕМИМ повідомленням-відповіддю (reply) на
    оригінальне повідомлення з відео в чаті — тап на цитату над повідомленням
    перегортає чат прямо до самого відео, щоб можна було освіжити в пам'яті,
    що там за контент, перед тим як публікувати. Кнопка "Запостити в
    Instagram" одразу публікує — те саме, що й у /publish_ig.
    """
    if not is_allowed(update):
        return

    videos = db.get_recent_tiktoks_for_instagram(limit=15)
    if not videos:
        await update.message.reply_text(
            "✅ Усі TikTok-відео вже опубліковані в Instagram (або ще жодного немає)."
        )
        return

    await update.message.reply_text(
        f"📋 Не опубліковано в Instagram: {len(videos)}.\n"
        "Тапни цитату під кожним повідомленням нижче, щоб перейти до відео в чаті."
    )

    for v in videos:
        caption_preview = (v.get("tiktok_caption") or "").strip().replace("\n", " ")[:120]
        published = (v.get("tiktok_published_at") or "")[:16]
        text = f"🎬 {published}\n{caption_preview or 'без підпису'}"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📸 Запостити в Instagram", callback_data=f"publish_ig:{v['id']}")
        ]])
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=markup,
                reply_to_message_id=v.get("message_id"),
            )
        except TgBadRequest:
            # Оригінальне повідомлення видалене/недоступне — надсилаємо без reply.
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=markup,
            )


def _build_tiktok_pending_page(offset: int) -> tuple:
    """Повертає (text, markup) для сторінки "Неопубліковані тіктоки"
    (tiktok_video_id IS NULL), offset=0 — перша сторінка. Кожен рядок —
    кнопка publish_tt:<id>, тап одразу викликає TikTok API
    (handle_publish_tiktok_callback). Ніякого автоматичного розкладу немає —
    власник сам вирішує, коли і яке відео відправити."""
    videos = db.get_unpublished_tiktok_videos(limit=PAGE_SIZE, offset=offset)
    if not videos:
        text = "✅ Усі оброблені відео вже відправлені в TikTok." if offset == 0 else "Більше немає."
        return text, None

    buttons = []
    for v in videos:
        caption_preview = (v.get("tiktok_caption") or "").strip().replace("\n", " ")[:36]
        created = (v.get("created_at") or "")[:16]
        label = f"{created} · {caption_preview or 'без підпису'}"
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"publish_tt:{v['id']}")])
    if len(videos) == PAGE_SIZE:
        buttons.append([InlineKeyboardButton("▶️ Показати ще", callback_data=f"tp_page:{offset + PAGE_SIZE}")])

    count_label = f"(показано з {offset + 1})" if offset else f"{len(videos)}+" if len(videos) == PAGE_SIZE else str(len(videos))
    return f"📋 Ще не відправлено в TikTok {count_label}.\nОбери яке відправити:", InlineKeyboardMarkup(buttons)


async def cmd_tiktok_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text, markup = _build_tiktok_pending_page(0)
    await update.message.reply_text(text, reply_markup=markup)


async def handle_tiktok_pending_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    offset = int(query.data.split(":", 1)[1])
    text, markup = _build_tiktok_pending_page(offset)
    await query.edit_message_text(text, reply_markup=markup)


def _video_topic(video: dict) -> str:
    """Коротка "тема" відео для списків: підпис TikTok, якщо є, інакше
    перші символи транскрипції (підпис міг не згенеруватись — див.
    /tiktok_pending, де багато старих відео позначені "без підпису")."""
    caption = (video.get("tiktok_caption") or "").strip().replace("\n", " ")
    if caption:
        return caption
    transcript = (video.get("transcript") or "").strip().replace("\n", " ")
    if transcript:
        return transcript
    return "без теми"


def _build_tiktok_sent_page(offset: int) -> tuple:
    """Повертає (text, markup) для сторінки "Відправлені тіктоки"
    (tiktok_video_id IS NOT NULL), найновіші першими. Кнопка "🔁" на кожне —
    publish_tt:<id>, той самий handle_publish_tiktok_callback, що й для
    першої відправки з /tiktok_pending (guard "вже відправлено" там навмисно
    вимкнено — саме щоб цей resend працював)."""
    videos = db.get_sent_tiktok_videos(limit=PAGE_SIZE, offset=offset)
    if not videos:
        text = "Ще жодного відео не відправлено в TikTok." if offset == 0 else "Більше немає."
        return text, None

    lines = [f"✅ Відправлено в TikTok (з {offset + 1}):\n" if offset else "✅ Відправлено в TikTok:\n"]
    buttons = []
    for i, v in enumerate(videos, start=offset + 1):
        sent_at = (v.get("tiktok_published_at") or "")[:16]
        topic = _video_topic(v)[:80]
        ig_mark = " · 📸 вже в IG" if v.get("instagram_media_id") else ""
        lines.append(f"{i}. 🎬 {sent_at} — {topic}{ig_mark}")
        button_label = f"🔁 {i}. {_video_topic(v)[:40]}"
        buttons.append([InlineKeyboardButton(button_label[:64], callback_data=f"publish_tt:{v['id']}")])
    if len(videos) == PAGE_SIZE:
        buttons.append([InlineKeyboardButton("▶️ Показати ще", callback_data=f"ts_page:{offset + PAGE_SIZE}")])

    text = "\n".join(lines) + "\n\n🔁 — відправити те саме відео в TikTok ще раз:"
    return text, InlineKeyboardMarkup(buttons)


async def cmd_tiktok_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text, markup = _build_tiktok_sent_page(0)
    await update.message.reply_text(text, reply_markup=markup)


async def handle_tiktok_sent_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    offset = int(query.data.split(":", 1)[1])
    text, markup = _build_tiktok_sent_page(offset)
    await query.edit_message_text(text, reply_markup=markup)


def _build_ig_sent_page(offset: int) -> tuple:
    """Повертає (text, markup) для сторінки "Опубліковані Reels"
    (instagram_media_id IS NOT NULL), найновіші першими. Кнопка "🔁" —
    publish_ig:<id>, той самий handle_publish_ig_callback (resend = другий
    пост-дублікат, свідомо — див. коментар там)."""
    videos = db.get_sent_instagram_videos(limit=PAGE_SIZE, offset=offset)
    if not videos:
        text = "Ще жодне відео не опубліковано в Instagram." if offset == 0 else "Більше немає."
        return text, None

    lines = [f"📸 Опубліковано в Instagram (з {offset + 1}):\n" if offset else "📸 Опубліковано в Instagram:\n"]
    buttons = []
    for i, v in enumerate(videos, start=offset + 1):
        sent_at = (v.get("instagram_published_at") or "")[:16]
        topic = _video_topic(v)[:80]
        lines.append(f"{i}. 📸 {sent_at} — {topic}")
        button_label = f"🔁 {i}. {_video_topic(v)[:40]}"
        buttons.append([InlineKeyboardButton(button_label[:64], callback_data=f"publish_ig:{v['id']}")])
    if len(videos) == PAGE_SIZE:
        buttons.append([InlineKeyboardButton("▶️ Показати ще", callback_data=f"is_page:{offset + PAGE_SIZE}")])

    text = "\n".join(lines) + "\n\n🔁 — опублікувати те саме відео в Instagram ще раз (буде ДРУГИЙ пост):"
    return text, InlineKeyboardMarkup(buttons)


async def cmd_ig_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text, markup = _build_ig_sent_page(0)
    await update.message.reply_text(text, reply_markup=markup)


async def handle_ig_sent_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    offset = int(query.data.split(":", 1)[1])
    text, markup = _build_ig_sent_page(offset)
    await query.edit_message_text(text, reply_markup=markup)


STATS_TOP_N = 3


async def _build_stats_page(offset: int) -> tuple:
    """Повертає (text, markup) для сторінки /stats. 🔥-рекомендація (топ-3 за
    переглядами) рахується лише в межах ПЕРШОЇ сторінки (offset=0) — це і так
    лише топ серед останніх PAGE_SIZE кандидатів, не буквально всіх видео за
    весь час; наступні сторінки просто показують статистику без 🔥, щоб не
    дублювати "топ-3" ярлик на кожній сторінці."""
    videos = db.get_recent_tiktoks_for_instagram(limit=PAGE_SIZE, offset=offset)
    if not videos:
        text = (
            "Немає відео, закинутих у TikTok, які ще не опубліковані в Instagram."
            if offset == 0 else "Більше немає."
        )
        return text, None

    videos = await asyncio.to_thread(_attach_tiktok_views, videos)
    matched = [v for v in videos if v.get("tiktok_public_views") is not None]
    unmatched = [v for v in videos if v.get("tiktok_public_views") is None]
    matched.sort(key=lambda v: -(v.get("tiktok_public_views") or 0))

    if not matched:
        text = (
            "Жодне з невиданих в Instagram TikTok-відео ще не зіставлено з "
            "публічним TikTok-акаунтом (усі ще в чернетках або зіставлення не "
            "вдалось). Публікуй у TikTok в застосунку — статистика підтягнеться "
            "після цього."
        )
        return text, None

    header = "📊 Перегляди/лайки/коменти TikTok (не опубліковано в Instagram):\n"
    lines = [header if offset == 0 else f"{header.rstrip()} (з {offset + 1}):\n"]
    buttons = []
    for i, v in enumerate(matched):
        caption_preview = (v.get("tiktok_caption") or "").strip().replace("\n", " ")[:40]
        views = v.get("tiktok_public_views") or 0
        likes = v.get("tiktok_public_likes")
        comments = v.get("tiktok_public_comments")
        stats_str = f"👁{_format_views(views)}"
        if likes is not None:
            stats_str += f" ❤️{_format_views(likes)}"
        if comments is not None:
            stats_str += f" 💬{_format_views(comments)}"
        recommended = offset == 0 and i < STATS_TOP_N
        prefix = "🔥 " if recommended else "• "
        lines.append(f"{prefix}{stats_str} · {caption_preview or 'без підпису'}")
        if recommended:
            label = f"🔥 {stats_str} · {caption_preview or 'без підпису'}"
            buttons.append([InlineKeyboardButton(label[:64], callback_data=f"publish_ig:{v['id']}")])

    if unmatched:
        lines.append(f"\n❔ Ще {len(unmatched)} відео не зіставлено з публічним TikTok (в чернетках або похибка зіставлення).")
    if offset == 0:
        lines.append("\n🔥 — топ-3 за переглядами серед неопублікованих в Instagram. Раджу запостити:")
    if len(videos) == PAGE_SIZE:
        buttons.append([InlineKeyboardButton("▶️ Показати ще", callback_data=f"st_page:{offset + PAGE_SIZE}")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Кнопка "Статистика" — реальні перегляди/лайки/коменти TikTok-відео, ще
    не опублікованих в Instagram, і рекомендація: топ-3 за переглядами
    позначаються як "залетіло" — кандидати на Instagram Reels.
    """
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("🔎 Перевіряю реальні перегляди/лайки/коменти в TikTok...")
    text, markup = await _build_stats_page(0)
    await msg.edit_text(text, reply_markup=markup)


async def handle_stats_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    offset = int(query.data.split(":", 1)[1])
    await query.edit_message_text("🔎 Перевіряю реальні перегляди/лайки/коменти в TikTok...")
    text, markup = await _build_stats_page(offset)
    await query.edit_message_text(text, reply_markup=markup)


async def cmd_test_ig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Безпечна перевірка: чи ПРАЦЮВАТИМЕ публікація в Instagram — БЕЗ реальної
    публікації (нічого не з'явиться на профілі/в стрічці). Див.
    publishers/instagram.py:test_connection() для деталей, що саме
    перевіряється.
    """
    if not is_allowed(update):
        return

    msg = await update.message.reply_text("🧪 Перевіряю підключення до Instagram Graph API (нічого не публікую)...")

    try:
        result = await asyncio.to_thread(ig_test_connection)
    except Exception as e:
        logger.error(f"Instagram test_connection failed: {e}", exc_info=True)
        await msg.edit_text(
            f"❌ Публікація в Instagram НЕ спрацює зараз:\n\n{e}\n\n"
            "Найчастіша причина — токен ще не отриманий/протух: пройди "
            "/auth/instagram/login."
        )
        return

    await msg.edit_text(
        "✅ Публікація в Instagram спрацює.\n\n"
        f"ig_user_id: {result['ig_user_id']}\n"
        f"container_id: {result['container_id']} (тестовий, НЕ опубліковано — "
        "сам згорить за ~24 год)\n\n"
        "Токен валідний, дозволи є, Graph API доступний. Можеш сміливо "
        "користуватись /ig_pending або /publish_ig."
    )


async def handle_publish_ig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Кнопка "📸 Запостити в Instagram" / "🔁 Відправити повторно"
    (publish_ig:<video_id>) — теж без guard "вже опубліковано", навмисно:
    той самий хендлер обслуговує і першу публікацію (з /ig_pending,
    /publish_ig, /stats, нагадувань), і resend з "📸 Опубліковані Reels"
    (cmd_ig_sent). На відміну від TikTok-inbox-флоу тут publish_reel —
    РЕАЛЬНА публікація в стрічку, тож resend створює ДРУГИЙ пост-дублікат —
    свідомий компроміс на прохання власника, а не помилка.
    """
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    video_id = int(query.data.split(":", 1)[1])
    video = db.get_video_by_id(video_id)
    if not video:
        await query.edit_message_text("❌ Відео не знайдено.")
        return
    is_resend = bool(video.get("instagram_media_id"))

    await query.edit_message_text(
        "⏳ Публікую повторно в Instagram Reels..." if is_resend else "⏳ Публікую в Instagram Reels..."
    )

    insta_caption = adapt_caption_for_instagram(video.get("tiktok_caption", ""))
    # s3_url_instagram — варіант з іншим стилем субтитрів (не TikTok-стиль),
    # щоб Instagram не порахував відео дублікатом TikTok-публікації і не
    # порізав охоплення. Fallback на s3_url для старих записів без варіанту.
    video_url = video.get("s3_url_instagram") or video["s3_url"]
    try:
        media_id = publish_reel(
            video_url=video_url,
            caption=insta_caption,
            cover_url=video.get("cover_s3_url"),
        )
        db.set_instagram_published(video_id, media_id, insta_caption)
        prefix = "✅ ПОВТОРНО опубліковано" if is_resend else "✅ Опубліковано"
        await query.edit_message_text(f"{prefix} в Instagram Reels (media_id={media_id}).")
    except Exception as e:
        logger.error(f"Помилка публікації в Instagram: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Помилка публікації в Instagram: {e}")


async def handle_publish_tiktok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Кнопка "📤 Відправити в TikTok" / "🔁 Відправити повторно"
    (publish_tt:<video_id>) — ручний тригер, єдиний спосіб потрапити в
    TikTok тепер (жодного автоматичного розкладу). Той самий хендлер і для
    першої відправки (з /tiktok_pending), і для повторної (з /tiktok_sent,
    tiktok_video_id вже заповнений) — навмисно без guard "вже відправлено":
    повторна відправка це нове завантаження в inbox, TikTok не заперечує
    проти дублікатів, і set_tiktok_published просто перезапише
    tiktok_video_id/tiktok_published_at на щойно відправлене.

    Викликає TikTok API напряму (publishers.tiktok.publish_video), що
    закидає відео у TikTok-inbox (чернетки) — власник однаково має сам
    відкрити застосунок і тапнути "Опублікувати" (обмеження платформи,
    Direct Post API без App Review недоступний). asyncio.to_thread — бо
    publish_video чекає на PUBLISH_COMPLETE до ~6.7 хв (_wait_for_publish),
    інакше заблокувало б увесь бот на цей час.
    """
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    video_id = int(query.data.split(":", 1)[1])
    video = db.get_video_by_id(video_id)
    if not video:
        await query.edit_message_text("❌ Відео не знайдено.")
        return
    is_resend = bool(video.get("tiktok_video_id"))

    await query.edit_message_text(
        "⏳ Відправляю повторно в TikTok (може зайняти кілька хвилин)..."
        if is_resend else
        "⏳ Відправляю в TikTok (може зайняти кілька хвилин)..."
    )

    caption = video.get("tiktok_caption") or ""
    try:
        tiktok_video_id = await asyncio.to_thread(
            tiktok_publish_video,
            video_url=video["s3_url"],
            caption=caption,
            cover_image_url=video.get("cover_s3_url"),
        )
        db.set_tiktok_published(video_id, tiktok_video_id, caption)
        intro = "🎬 Відео ПОВТОРНО завантажилось у TikTok-чернетки!" if is_resend else "🎬 Відео завантажилось у TikTok-чернетки!"
        await query.edit_message_text(
            f"{intro}\n\n"
            "Відкрий TikTok і натисни «Опублікувати».\n\n"
            "Коли будеш готовий — можна одразу закинути це відео в Instagram Reels:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📸 Запостити в Instagram", callback_data=f"publish_ig:{video_id}")
            ]]),
        )
        # При повторній відправці обкладинку й підпис НЕ шлемо наново (щоб не
        # смітити в чаті) — замість цього коротке reply-посилання на
        # оригінальне повідомлення з відео, під яким вони вже лежать.
        if is_resend and video.get("chat_id") and video.get("message_id"):
            try:
                await context.bot.send_message(
                    chat_id=video["chat_id"],
                    text="📋 Обкладинка і підпис — дивись повідомлення під оригінальним відео вище ⬆️",
                    reply_to_message_id=video["message_id"],
                )
            except TgBadRequest:
                pass  # оригінальне повідомлення видалене/недоступне — пропускаємо
    except Exception as e:
        logger.error(f"Помилка відправки в TikTok: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Помилка відправки в TikTok: {e}")


# ── Невдала обробка відео ─────────────────────────────────────────────────────

def _build_failed_videos_page(offset: int) -> tuple:
    """Повертає (text, markup) для сторінки "Невдалі відео" (failed_videos,
    resolved=0), найновіші першими. Кнопка "🔁 Спробувати ще раз" —
    retry_failed:<id> (handle_retry_failed_callback)."""
    items = db.get_failed_videos(limit=PAGE_SIZE, offset=offset)
    if not items:
        text = "✅ Немає відео з невдалою обробкою." if offset == 0 else "Більше немає."
        return text, None

    lines = [f"⚠️ Невдала обробка (з {offset + 1}):\n" if offset else "⚠️ Невдала обробка:\n"]
    buttons = []
    for i, f in enumerate(items, start=offset + 1):
        failed_at = (f.get("failed_at") or "")[:16]
        error_preview = (f.get("error") or "").strip().replace("\n", " ")[:100]
        name = f.get("original_filename") or f["source_ref"]
        lines.append(f"{i}. ❌ {failed_at} — {name}\n   {error_preview}")
        label = f"🔁 {i}. {name}"
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"retry_failed:{f['id']}")])
    if len(items) == PAGE_SIZE:
        buttons.append([InlineKeyboardButton("▶️ Показати ще", callback_data=f"fv_page:{offset + PAGE_SIZE}")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_failed_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text, markup = _build_failed_videos_page(0)
    await update.message.reply_text(text, reply_markup=markup)


async def handle_failed_videos_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    offset = int(query.data.split(":", 1)[1])
    text, markup = _build_failed_videos_page(offset)
    await query.edit_message_text(text, reply_markup=markup)


async def handle_retry_failed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Кнопка "🔁 Спробувати ще раз" (retry_failed:<failed_id>) — перезавантажує
    файл із джерела (telegram file_id / drive file_id / url — джерело
    зберігається в failed_videos.source_type/source_ref, бо локальний файл
    на момент провалу вже видалений у finally) і заново запускає пайплайн
    через _process_drive_file (Update-агностична — працює й тут, і для
    Drive-поллера).

    Рядок resolved одразу (оптимістично, ДО повторної спроби): якщо retry
    знову впаде — except у _process_drive_file створить НОВИЙ запис
    failed_videos зі свіжою помилкою, замість накопичення дублів на
    той самий відеофайл.
    """
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    failed_id = int(query.data.split(":", 1)[1])
    fv = db.get_failed_video_by_id(failed_id)
    if not fv:
        await query.edit_message_text("❌ Запис не знайдено (можливо, вже прибрано).")
        return

    db.resolve_failed_video(failed_id)
    await query.edit_message_text(f"🔁 Повторно завантажую «{fv['original_filename']}»...")
    chat_id = fv.get("chat_id") or TELEGRAM_ALLOWED_USER_ID

    try:
        if fv["source_type"] == "telegram":
            file = await context.bot.get_file(fv["source_ref"])
            local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_retry.mp4")
            await file.download_to_drive(local_path)
        elif fv["source_type"] == "drive":
            local_path = await asyncio.to_thread(drive_download, fv["source_ref"], fv["original_filename"])
        elif fv["source_type"] == "url":
            local_path = await asyncio.to_thread(_download_direct_url, fv["source_ref"])
        else:
            raise RuntimeError(f"Невідоме джерело: {fv['source_type']}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Не вдалось повторно завантажити: {e}")
        db.log_failed_video(fv["original_filename"], fv["source_type"], fv["source_ref"], str(e), chat_id=chat_id)
        return

    retry_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Обробляю повторно...")
    await _process_drive_file(
        context.application, chat_id, retry_msg, local_path, fv["original_filename"],
        source_type=fv["source_type"], source_ref=fv["source_ref"],
    )


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


# ── Instagram карусель (сторітейл) ───────────────────────────────────────────
#
# Сценарій: надсилаєш кілька фото ОДНИМ альбомом у Telegram (готові слайди,
# з накладеним текстом — підготовлені заздалегідь, напр. в Claude Desktop).
# Бот збирає весь альбом (Telegram доставляє фото альбому окремими updates —
# доводиться "дебаунсити"), вивантажує на S3, питає підпис, тоді час
# публікації — і публікує каруселлю в Instagram Graph API за розкладом
# (автопублікація, без додаткового підтвердження в момент публікації, як і
# для TikTok-черги: вибір часу на цьому кроці і Є підтвердженням).

ALBUM_DEBOUNCE_SECONDS = 1.5
_album_debounce_jobs: dict = {}  # media_group_id → Job (модульний рівень: не залежить від chat_data)


async def handle_carousel_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приймає фото (стиснуті `filters.PHOTO` або оригінальні `filters.Document.IMAGE`),
    накопичує в буфер по media_group_id і через ALBUM_DEBOUNCE_SECONDS після
    останнього фото альбому запускає _finalize_carousel_album."""
    if not is_allowed(update):
        return

    msg = update.message
    file_id = None
    if msg.photo:
        file_id = msg.photo[-1].file_id  # найбільша доступна роздільність
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id

    if not file_id:
        return

    group_id = msg.media_group_id or f"single-{msg.chat_id}-{msg.message_id}"

    pending = context.chat_data.setdefault("pending_carousel_albums", {})
    pending.setdefault(group_id, []).append(file_id)

    # Дебаунс: Telegram присилає фото одного альбому окремими updates майже
    # одночасно — кожне нове фото скасовує попередній таймер і ставить новий,
    # щоб "фіналізація" спрацювала рівно один раз, після останнього фото.
    existing_job = _album_debounce_jobs.get(group_id)
    if existing_job:
        existing_job.schedule_removal()

    job = context.job_queue.run_once(
        _finalize_carousel_album,
        when=ALBUM_DEBOUNCE_SECONDS,
        chat_id=msg.chat_id,
        data={"group_id": group_id},
        name=f"finalize_album_{group_id}",
    )
    _album_debounce_jobs[group_id] = job


async def _finalize_carousel_album(context: ContextTypes.DEFAULT_TYPE):
    """Job callback (запускається через job_queue з chat_id=... — тому
    context.chat_data тут прив'язаний до того самого чату, що й у хендлері
    handle_carousel_photos вище)."""
    group_id = context.job.data["group_id"]
    chat_id = context.job.chat_id
    _album_debounce_jobs.pop(group_id, None)

    pending = context.chat_data.setdefault("pending_carousel_albums", {})
    file_ids = pending.pop(group_id, [])
    if not file_ids:
        return

    if len(file_ids) < 2:
        await context.bot.send_message(
            chat_id=chat_id,
            text="📸 Отримав 1 фото. Для сторітейл-каруселі потрібно мінімум 2 слайди — "
                 "надішли решту одним альбомом (вибрати кілька фото одразу в Telegram).",
        )
        return
    if len(file_ids) > 10:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📸 Отримав {len(file_ids)} фото, але Instagram-карусель приймає максимум 10 слайдів. "
                 "Надішли не більше 10.",
        )
        return

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"📸 Отримав {len(file_ids)} фото. Завантажую слайди на S3...",
    )

    image_urls = []
    try:
        for i, file_id in enumerate(file_ids):
            file = await context.bot.get_file(file_id)
            local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_slide{i}.jpg")
            await file.download_to_drive(local_path)
            try:
                url = await asyncio.to_thread(upload_file, local_path, "carousel")
                image_urls.append(url)
            finally:
                os.remove(local_path)
    except Exception as e:
        logger.error(f"Помилка завантаження слайдів каруселі: {e}", exc_info=True)
        await msg.edit_text(f"❌ Помилка завантаження слайдів: {e}")
        return

    carousel_id = db.create_carousel(image_urls, caption="")
    context.chat_data["pending_carousel_id"] = carousel_id

    await msg.edit_text(
        f"✅ {len(image_urls)} слайдів завантажено (карусель #{carousel_id}).\n\n"
        "Надішли підпис текстом — або /nocap, щоб опублікувати без підпису."
    )


async def cmd_nocap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропускає крок підпису для каруселі, що очікує на нього."""
    if not is_allowed(update):
        return
    carousel_id = context.chat_data.pop("pending_carousel_id", None)
    if not carousel_id:
        await update.message.reply_text("Немає каруселі, що очікує на підпис.")
        return
    await update.message.reply_text(
        f"Карусель #{carousel_id} без підпису.\n\nКоли публікуємо в Instagram?",
        reply_markup=_build_carousel_schedule_keyboard(carousel_id),
    )


async def handle_carousel_caption_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вільний текст обробляємо ЛИШЕ якщо є карусель, що очікує на підпис —
    інакше нічого не робимо (щоб не заважати іншим сценаріям бота)."""
    if not is_allowed(update):
        return
    carousel_id = context.chat_data.get("pending_carousel_id")
    if not carousel_id:
        return

    caption = (update.message.text or "").strip()
    context.chat_data.pop("pending_carousel_id", None)
    db.set_carousel_caption(carousel_id, caption)

    await update.message.reply_text(
        f"📝 Підпис збережено для каруселі #{carousel_id}.\n\nКоли публікуємо в Instagram?",
        reply_markup=_build_carousel_schedule_keyboard(carousel_id),
    )


def _build_carousel_schedule_keyboard(carousel_id: int) -> InlineKeyboardMarkup:
    """Кнопки планування для каруселі: дефолтна година з INSTAGRAM_PUBLISH_HOUR,
    ті самі слоти що й для TikTok (для зручності — один спільний ритм публікацій),
    і "Зараз". Час у callback_data — "HH:MM" за Києвом як є, конвертація в
    UTC у момент кліку (handle_carousel_schedule_callback через
    _next_kyiv_time), а не тут — щоб кнопка не "застарівала"."""
    default_time_str = f"{INSTAGRAM_PUBLISH_HOUR:02d}:00"
    buttons = [[InlineKeyboardButton(
        f"🌅 {default_time_str}",
        callback_data=f"schedule_carousel:{carousel_id}:{default_time_str}",
    )]]

    for time_str in TIKTOK_PUBLISH_TIMES:
        buttons.append([InlineKeyboardButton(
            f"🕐 {time_str}",
            callback_data=f"schedule_carousel:{carousel_id}:{time_str}",
        )])

    buttons.append([InlineKeyboardButton(
        "🔴 Зараз", callback_data=f"schedule_carousel:{carousel_id}:now",
    )])
    return InlineKeyboardMarkup(buttons)


async def handle_carousel_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    _, carousel_id_str, time_part = query.data.split(":", 2)
    carousel_id = int(carousel_id_str)

    if time_part == "now":
        scheduled_at = datetime.now()
        label = "зараз"
    else:
        h, m = map(int, time_part.split(":"))
        scheduled_at, day_label = _next_kyiv_time(h, m)
        label = f"{time_part} за Києвом ({day_label})"

    db.enqueue_carousel(carousel_id, scheduled_at)

    await query.edit_message_text(
        f"✅ Карусель #{carousel_id} поставлено в чергу на {label}.\n"
        "Опублікується автоматично в Instagram — підтверджувати повторно не треба."
    )


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
            source_type, source_ref = "drive", file_id
        else:
            local_path = await asyncio.to_thread(_download_direct_url, url)
            source_type, source_ref = "url", url
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось завантажити відео: {e}")
        return

    await _process_video_file(update, context, msg, local_path, source_type=source_type, source_ref=source_ref)


def _download_direct_url(url: str) -> str:
    """Синхронне завантаження прямого посилання на mp4 (виконується через
    asyncio.to_thread). Винесено з cmd_process_url окремою функцією, щоб те
    саме завантаження можна було повторити з "⚠️ Невдалі відео" →
    "🔁 Спробувати ще раз" (handle_retry_failed_callback)."""
    local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_raw.mp4")
    resp = http_requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_path


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
    await _process_video_file(
        update, context, msg, local_path, original_name,
        source_type="telegram", source_ref=video.file_id,
    )


async def _auto_publish_youtube(video_id: int, local_video_path: str, title: str, description: str) -> str:
    """
    Автопублікація YouTube Shorts — ПОВНІСТЮ автоматично й публічно, без
    жодної кнопки (на відміну від TikTok/Instagram): YouTube Data API
    дозволяє справжню публікацію для особистого каналу без App Review, тому
    механізм "чернетка + ручний тап" тут просто не потрібен.

    Мовчки пропускає (повертає ""), якщо YOUTUBE_CLIENT_ID не задано —
    інтеграція ще не підключена (/auth/youtube/login) — решта пайплайну
    (TikTok/Instagram) при цьому не ламається.

    Повертає готовий рядок для фінального повідомлення (посилання на Short
    або текст помилки) — щоб не губити провал мовчки: показуємо власнику
    навіть якщо публікація в YouTube не вдалась.
    """
    if not YOUTUBE_CLIENT_ID:
        return ""
    try:
        yt_video_id = await asyncio.to_thread(youtube_publish_short, local_video_path, title, description)
        db.set_youtube_published(video_id, yt_video_id)
        return f"📺 YouTube Shorts: https://youtube.com/shorts/{yt_video_id}\n\n"
    except Exception as e:
        logger.warning(f"YouTube publish failed: {e}", exc_info=True)
        return f"📺 YouTube: не вдалось опублікувати ({e})\n\n"


async def _process_video_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    msg,
    local_path: str,
    original_name: str = "video.mp4",
    source_type: str = "telegram",
    source_ref: str = "",
):
    """Спільний пайплайн обробки відео для handle_video і cmd_process_url.

    source_type/source_ref — звідки взяти файл ЗАНОВО, якщо обробка впаде
    (telegram file_id / drive file_id / url) — записується в failed_videos
    (див. except нижче), щоб "⚠️ Невдалі відео" → "🔁 Спробувати ще раз"
    (handle_retry_failed_callback) міг перезавантажити той самий файл
    (локальний local_path на той момент уже видалено в finally)."""
    std_path = None
    no_silence_path = None
    vertical_path = None
    ass_paths = []
    final_video_paths = {}
    frame_path = None
    cover_path = None
    tiktok_caption = None

    try:
        # 0. Конвертуємо до стандартного H.264/AAC (MOV, HEVC, VFR → mp4 30fps)
        await msg.edit_text("🔄 Конвертую формат відео...")
        std_path = await asyncio.to_thread(to_standard_mp4, local_path)

        # 1. Видалення пауз
        await msg.edit_text("✂️ Видаляю паузи...")
        no_silence_path = await asyncio.to_thread(remove_silence, std_path)

        # 2. Транскрипція (один виклик Whisper/AAI — рендеримо в кілька
        #    стилів субтитрів нижче без повторних запитів до платного API)
        await msg.edit_text("📝 Транскрибую відео...")
        word_tuples, segments, transcript = await asyncio.to_thread(transcribe_words, no_silence_path)

        # 3. Нормалізація до 9:16
        await msg.edit_text("📐 Приводжу відео до 9:16...")
        vertical_path = normalize_vertical(no_silence_path)

        # 4. Burn-in субтитрів — ОКРЕМЕ відео на TikTok і на Instagram (інший
        #    колір/розмір/позиція тексту), щоб платформи не розпізнали їх як
        #    дублікат одна одної і не порізали охоплення за crossposting-
        #    детекцією. Одна транскрипція → 2 рендери (build_ass_for_style),
        #    без повторного виклику Whisper.
        if transcript and transcript.strip():
            for platform in ("tiktok", "instagram"):
                await msg.edit_text(f"🎬 Накладаю субтитри ({platform})...")
                ass_content = build_ass_for_style(word_tuples, segments, platform)
                ass_path = save_ass(ass_content)
                ass_paths.append(ass_path)
                final_video_paths[platform] = await asyncio.to_thread(burn_subtitles, vertical_path, ass_path)
        else:
            await msg.edit_text("🎬 Мовлення не розпізнано — субтитри пропускаю...")
            final_video_paths["tiktok"] = vertical_path
            final_video_paths["instagram"] = vertical_path

        # 5. Обкладинка (кадр однаковий незалежно від стилю субтитрів)
        await msg.edit_text("🖼 Генерую обкладинку...")
        frame_path = extract_frame(final_video_paths["tiktok"], timestamp=1.5)
        cover_path = generate_cover(transcript, frame_path)

        # 6. Завантаження на S3 (2 відео + обкладинка)
        await msg.edit_text("☁️ Завантажую на S3...")
        s3_video_url = upload_file(final_video_paths["tiktok"], prefix="videos")
        s3_video_url_instagram = (
            upload_file(final_video_paths["instagram"], prefix="videos")
            if final_video_paths["instagram"] != final_video_paths["tiktok"]
            else s3_video_url
        )
        s3_cover_url = upload_file(cover_path, prefix="covers")

        # 7. Зберігаємо в БД (chat_id/message_id — щоб пізніше нагадування й
        #    /ig_pending могли послатись на це повідомлення як reply, тобто
        #    дати "перехід" до самого відео в чаті)
        video_id = db.create_video(
            original_filename=original_name,
            s3_url=s3_video_url,
            s3_url_instagram=s3_video_url_instagram,
            cover_s3_url=s3_cover_url,
            transcript=transcript,
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
        )

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

        # 8.5. YouTube Shorts — публікується ПОВНІСТЮ АВТОМАТИЧНО (публічно,
        #      без кнопки), на відміну від TikTok/Instagram. Local-файл ще не
        #      видалений (cleanup лише у finally нижче) — саме тому цей крок
        #      МАЄ бути до нього: YouTube API не вміє "pull from URL".
        await msg.edit_text("📺 Публікую в YouTube Shorts...")
        youtube_line = await _auto_publish_youtube(
            video_id, final_video_paths["tiktok"], tiktok_caption or original_name, transcript or "",
        )

        # 9. TikTok НЕ відправляється автоматично — власник сам тисне кнопку,
        #    коли захоче (тут одразу, або пізніше зі списку "📋 Неопубліковані
        #    тіктоки" — handle_publish_tiktok_callback).
        transcript_line = (
            f"📝 Транскрипція: <i>{html.escape(transcript[:100])}...</i>\n\n"
            if transcript and transcript.strip()
            else "📝 Мовлення не розпізнано (без субтитрів)\n\n"
        )
        await msg.edit_text(
            f"✅ Відео готове!\n\n{transcript_line}{youtube_line}"
            "Тисни кнопку, коли захочеш відправити в TikTok:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📤 Відправити в TikTok", callback_data=f"publish_tt:{video_id}")
            ]]),
        )

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        if source_ref:
            db.log_failed_video(
                original_name, source_type, source_ref, str(e),
                chat_id=update.effective_chat.id,
                message_id=getattr(msg, "message_id", None),
            )
            await msg.edit_text(
                f"❌ Помилка обробки: {e}\n\n"
                f"Додав у {BTN_FAILED} — можна спробувати ще раз кнопкою."
            )
        else:
            await msg.edit_text(f"❌ Помилка обробки: {e}")
    finally:
        cleanup_paths = [local_path, no_silence_path, vertical_path, *ass_paths, *final_video_paths.values(), frame_path, cover_path]
        for path in cleanup_paths:
            if not path:
                continue
            try:
                os.remove(path)
            except Exception:
                pass


_KYIV_TZ = ZoneInfo("Europe/Kyiv")


def _next_kyiv_time(hour: int, minute: int = 0) -> tuple:
    """
    Найближчий момент, коли за Києвом настане hour:minute (сьогодні, якщо
    ще не минуло, інакше завтра).

    Повертає (scheduled_at_utc_naive, day_label):
      scheduled_at_utc_naive — НАЇВНИЙ datetime у UTC (без tzinfo), щоб
        узгоджуватись з рештою черги: db.enqueue зберігає .isoformat(), а
        SQLite datetime('now') у db.get_pending_queue() завжди UTC —
        незалежно від того, у якому часовому поясі фактично працює
        контейнер на Railway (типово UTC за замовчуванням, без якого час,
        заданий у INSTAGRAM_PUBLISH_HOUR/INSTAGRAM_REELS_HOUR, фактично
        зсувався б на 2-3 год від задуманого київського).
      day_label — "сьогодні" або "завтра", для повідомлення в чаті.
    """
    now_kyiv = datetime.now(_KYIV_TZ)
    target_kyiv = now_kyiv.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day_label = "сьогодні"
    if target_kyiv <= now_kyiv:
        target_kyiv += timedelta(days=1)
        day_label = "завтра"
    scheduled_at_utc = target_kyiv.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return scheduled_at_utc, day_label


# ── Щоденне нагадування "опублікуй тіктоки" ──────────────────────────────────
#
# 18:00 за Балі (UTC+8, без переведення стрілок) = 13:00 за Києвом — вечір на
# Балі, робочий день в Україні/Європі, тож обидва часових пояси зручні.
# Спрацьовує ЛИШЕ якщо сьогодні опубліковано менше TIKTOK_DAILY_LIMIT —
# на відміну від scheduler/queue_runner.py:_send_tiktok_reminder (реактивне,
# одразу після кожної окремої ручної відправки в чернетки).

DAILY_REMINDER_HOUR_KYIV = 13
DAILY_REMINDER_MINUTE_KYIV = 0


async def _daily_tiktok_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    published_today = db.count_tiktoks_today()
    if published_today >= TIKTOK_DAILY_LIMIT:
        return  # ціль на сьогодні вже досягнута — нагадувати нема про що

    remaining = TIKTOK_DAILY_LIMIT - published_today
    await context.bot.send_message(
        chat_id=TELEGRAM_ALLOWED_USER_ID,
        text=(
            f"🔔 Нагадування: сьогодні опубліковано лише {published_today}/{TIKTOK_DAILY_LIMIT} "
            f"тіктоків, ще потрібно щонайменше {remaining}.\n\n"
            "Перевір «📋 Неопубліковані тіктоки» і відправ решту."
        ),
        reply_markup=_main_menu_keyboard(),
    )


# ── Періодичне нагадування "обери контент для Instagram" ─────────────────────
#
# Раз на ~3 дні (не за job_queue-розкладом — той губиться при кожному
# редеплої — а за станом у БД, db.get_last_reminder_at, щоб каданс пережив
# рестарт), і ЛИШЕ якщо є що показати: неопубліковані в Instagram TikTok-
# відео і/або каруселі, доведені до половини (створені, але без підпису/часу
# публікації). Перевіряється щодня о 12:00 Києва (окремо від 13:00 TikTok-
# нагадування, щоб не приходили обидва одночасно).

IG_REMINDER_HOUR_KYIV = 12
IG_REMINDER_MINUTE_KYIV = 0
IG_REMINDER_MIN_DAYS = 3
IG_REMINDER_KIND = "ig_choose_reel"


async def _ig_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    last_sent = db.get_last_reminder_at(IG_REMINDER_KIND)
    if last_sent and (datetime.now() - last_sent) < timedelta(days=IG_REMINDER_MIN_DAYS):
        return

    pending_reels = db.count_pending_instagram_reels()
    unscheduled_carousels = db.get_unscheduled_carousels(limit=5)
    if not pending_reels and not unscheduled_carousels:
        return  # нема чого нагадувати — не логуємо відправку, спробуємо знову завтра

    lines = ["🔔 Нагадування (раз на кілька днів):\n"]
    if pending_reels:
        lines.append(f"🎬 {pending_reels} TikTok-відео ще не в Instagram Reels — обери одне в «{BTN_IG_PENDING}».")
    if unscheduled_carousels:
        lines.append(
            f"📚 {len(unscheduled_carousels)} карусель(і) створено, але не доведено до кінця "
            "(без підпису/часу публікації) — доверши через /nocap або підпис текстом."
        )

    await context.bot.send_message(
        chat_id=TELEGRAM_ALLOWED_USER_ID,
        text="\n".join(lines),
        reply_markup=_main_menu_keyboard(),
    )
    db.log_reminder_sent(IG_REMINDER_KIND)


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


async def _process_drive_file(
    app, chat_id: int, msg, local_path: str, filename: str, file_id: str = "",
    source_type: str = "drive", source_ref: str = None,
):
    """
    Запускає пайплайн обробки для файлу з Drive (або будь-якого джерела —
    Update-агностична, тому й handle_retry_failed_callback перевикористовує
    її для повторної обробки telegram-/url-джерел теж) без реального
    telegram.Update. Надсилає результати напряму в chat_id.

    source_type/source_ref — як і в _process_video_file, для запису в
    failed_videos при провалі (за замовчуванням "drive"/file_id — типовий
    виклик з Drive-поллера/кнопки)."""
    source_ref = source_ref if source_ref is not None else file_id
    std_path = no_silence_path = vertical_path = None
    ass_paths = []
    final_video_paths = {}
    frame_path = cover_path = None
    tiktok_caption = None

    try:
        await msg.edit_text(f"🔄 «{filename}» — конвертую формат...")
        std_path = await asyncio.to_thread(to_standard_mp4, local_path)

        await msg.edit_text(f"✂️ «{filename}» — видаляю паузи...")
        no_silence_path = await asyncio.to_thread(remove_silence, std_path)

        await msg.edit_text(f"📝 «{filename}» — транскрибую...")
        word_tuples, segments, transcript = await asyncio.to_thread(transcribe_words, no_silence_path)

        await msg.edit_text(f"📐 «{filename}» — приводжу до 9:16...")
        vertical_path = await asyncio.to_thread(normalize_vertical, no_silence_path)

        # Окреме відео на TikTok і на Instagram (інший стиль субтитрів) —
        # див. коментар у _process_video_file вище.
        if transcript and transcript.strip():
            for platform in ("tiktok", "instagram"):
                await msg.edit_text(f"🎬 «{filename}» — накладаю субтитри ({platform})...")
                ass_content = build_ass_for_style(word_tuples, segments, platform)
                ass_path = await asyncio.to_thread(save_ass, ass_content)
                ass_paths.append(ass_path)
                final_video_paths[platform] = await asyncio.to_thread(burn_subtitles, vertical_path, ass_path)
        else:
            final_video_paths["tiktok"] = vertical_path
            final_video_paths["instagram"] = vertical_path

        await msg.edit_text(f"🖼 «{filename}» — генерую обкладинку...")
        frame_path = await asyncio.to_thread(extract_frame, final_video_paths["tiktok"], 1.5)
        cover_path = await asyncio.to_thread(generate_cover, transcript, frame_path)

        await msg.edit_text(f"☁️ «{filename}» — завантажую на S3...")
        s3_video_url = await asyncio.to_thread(upload_file, final_video_paths["tiktok"], "videos")
        s3_video_url_instagram = (
            await asyncio.to_thread(upload_file, final_video_paths["instagram"], "videos")
            if final_video_paths["instagram"] != final_video_paths["tiktok"]
            else s3_video_url
        )
        s3_cover_url = await asyncio.to_thread(upload_file, cover_path, "covers")

        video_id = db.create_video(
            original_filename=filename,
            s3_url=s3_video_url,
            s3_url_instagram=s3_video_url_instagram,
            cover_s3_url=s3_cover_url,
            transcript=transcript,
            chat_id=chat_id,
            message_id=msg.message_id,
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

        # YouTube Shorts — повністю автоматично, публічно, без кнопки. Див.
        # коментар у _auto_publish_youtube (main.py) вище.
        await msg.edit_text(f"📺 «{filename}» — публікую в YouTube Shorts...")
        youtube_line = await _auto_publish_youtube(
            video_id, final_video_paths["tiktok"], tiktok_caption or filename, transcript or "",
        )

        # TikTok НЕ відправляється автоматично — див. коментар у
        # _process_video_file вище.
        transcript_line = f"📝 <i>{html.escape(transcript[:100])}...</i>\n\n" if transcript and transcript.strip() else ""
        await msg.edit_text(
            f"✅ «{html.escape(filename)}» готове!\n\n{transcript_line}{youtube_line}"
            "Тисни кнопку, коли захочеш відправити в TikTok:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📤 Відправити в TikTok", callback_data=f"publish_tt:{video_id}")
            ]]),
        )

    except Exception as e:
        logger.error(f"Drive pipeline error: {e}", exc_info=True)
        if source_ref:
            db.log_failed_video(
                filename, source_type, source_ref, str(e),
                chat_id=chat_id, message_id=getattr(msg, "message_id", None),
            )
            await msg.edit_text(
                f"❌ Помилка обробки «{filename}»: {e}\n\n"
                f"Додав у {BTN_FAILED} — можна спробувати ще раз кнопкою."
            )
        else:
            await msg.edit_text(f"❌ Помилка обробки «{filename}»: {e}")
    finally:
        unmark_processing(file_id)
        cleanup_paths = [local_path, std_path, no_silence_path, vertical_path, *ass_paths, *final_video_paths.values(), frame_path, cover_path]
        for path in cleanup_paths:
            if not path:
                continue
            try:
                os.remove(path)
            except Exception:
                pass


# ── Main ──────────────────────────────────────────────────────────────────────

async def _post_init(app: Application):
    """
    Реєструє команди в Telegram (з'являються в меню "/" при наборі) —
    інакше команди ПРАЦЮЮТЬ, але їх ніде не видно, поки не знаєш точну
    назву напам'ять (саме тому /ig_pending лишався непоміченим).
    """
    await app.bot.set_my_commands([
        BotCommand("start", "Довідка — як користуватись ботом"),
        BotCommand("status", "Скільки опубліковано в TikTok сьогодні"),
        BotCommand("queue", "Заплановані Instagram-каруселі"),
        BotCommand("tiktok_pending", "Оброблені відео, ще не відправлені в TikTok"),
        BotCommand("tiktok_sent", "Відео, вже відправлені в TikTok (є «відправити повторно»)"),
        BotCommand("ig_pending", "TikTok-відео, ще не опубліковані в Instagram (з переходом у чат)"),
        BotCommand("ig_sent", "Відео, вже опубліковані в Instagram (є «відправити повторно»)"),
        BotCommand("publish_ig", "Те саме, що ig_pending, одним списком з реальними переглядами TikTok"),
        BotCommand("stats", "Перегляди/лайки/коменти TikTok + що варто запостити в Instagram"),
        BotCommand("failed_videos", "Відео, де обробка впала (є «спробувати ще раз»)"),
        BotCommand("test_ig", "Перевірка підключення до Instagram (нічого не публікує)"),
        BotCommand("scan_drive", "Перевірити нові відео в Google Drive"),
        BotCommand("process_url", "Обробити відео за посиланням (якщо файл >20MB)"),
        BotCommand("nocap", "Опублікувати карусель без підпису"),
        BotCommand("dm_blast", "Розсилка в Instagram Direct"),
    ])


def main():
    db.init_db()

    # Запускаємо queue_runner в окремому потоці
    queue_thread = threading.Thread(target=queue_runner_run, daemon=True)
    queue_thread.start()
    logger.info("Queue runner запущено в фоні.")

    # Запускаємо веб-сервер з /terms і /privacy (для TikTok App Review)
    start_webapp()
    logger.info("Веб-сервер (/terms, /privacy) запущено в фоні.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("publish_ig", cmd_publish_ig))
    app.add_handler(CommandHandler("ig_pending", cmd_ig_pending))
    app.add_handler(CommandHandler("ig_sent", cmd_ig_sent))
    app.add_handler(CommandHandler("tiktok_pending", cmd_tiktok_pending))
    app.add_handler(CommandHandler("tiktok_sent", cmd_tiktok_sent))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("failed_videos", cmd_failed_videos))
    app.add_handler(CommandHandler("test_ig", cmd_test_ig))
    app.add_handler(CommandHandler("dm_blast", cmd_dm_blast))
    app.add_handler(CommandHandler("process_url", cmd_process_url))
    app.add_handler(CommandHandler("scan_drive", cmd_scan_drive))
    app.add_handler(CallbackQueryHandler(handle_drive_process_callback, pattern=r"^drive_process:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(CallbackQueryHandler(handle_publish_ig_callback, pattern=r"^publish_ig:"))
    app.add_handler(CallbackQueryHandler(handle_publish_tiktok_callback, pattern=r"^publish_tt:"))
    app.add_handler(CallbackQueryHandler(handle_retry_failed_callback, pattern=r"^retry_failed:"))
    app.add_handler(CallbackQueryHandler(handle_tiktok_pending_page_callback, pattern=r"^tp_page:"))
    app.add_handler(CallbackQueryHandler(handle_tiktok_sent_page_callback, pattern=r"^ts_page:"))
    app.add_handler(CallbackQueryHandler(handle_ig_sent_page_callback, pattern=r"^is_page:"))
    app.add_handler(CallbackQueryHandler(handle_stats_page_callback, pattern=r"^st_page:"))
    app.add_handler(CallbackQueryHandler(handle_failed_videos_page_callback, pattern=r"^fv_page:"))
    app.add_handler(CallbackQueryHandler(handle_dm_blast_callback, pattern=r"^dm_blast_(confirm|cancel)$"))
    app.add_handler(CommandHandler("nocap", cmd_nocap))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_carousel_photos))
    app.add_handler(CallbackQueryHandler(handle_carousel_schedule_callback, pattern=r"^schedule_carousel:"))
    # Постійне меню кнопок — текстові мітки кнопок ловимо ДО вільного тексту
    # каруселі нижче, інакше натискання кнопки сприймалось би за підпис.
    app.add_handler(MessageHandler(filters.Text([BTN_TIKTOK_PENDING]), cmd_tiktok_pending))
    app.add_handler(MessageHandler(filters.Text([BTN_TIKTOK_SENT]), cmd_tiktok_sent))
    app.add_handler(MessageHandler(filters.Text([BTN_IG_PENDING]), cmd_ig_pending))
    app.add_handler(MessageHandler(filters.Text([BTN_IG_SENT]), cmd_ig_sent))
    app.add_handler(MessageHandler(filters.Text([BTN_STATS]), cmd_stats))
    app.add_handler(MessageHandler(filters.Text([BTN_FAILED]), cmd_failed_videos))
    # Вільний текст — тільки для підпису каруселі, що очікує на нього (див.
    # handle_carousel_caption_text: якщо очікування немає — нічого не робить).
    # Реєструємо останнім, щоб не заважати командам вище.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_carousel_caption_text))

    # Drive poller: перевіряє папку кожні 2 хвилини
    if GOOGLE_DRIVE_FOLDER_ID:
        app.job_queue.run_repeating(
            _drive_poll_job,
            interval=DRIVE_POLL_INTERVAL,
            first=30,  # перша перевірка через 30с після старту
            name="drive_poller",
        )
        logger.info(f"Drive poller: перевірка кожні {DRIVE_POLL_INTERVAL}с.")

    # Щоденне нагадування "опублікуй тіктоки" — 13:00 за Києвом (18:00 Балі)
    app.job_queue.run_daily(
        _daily_tiktok_reminder_job,
        time=dt_time(hour=DAILY_REMINDER_HOUR_KYIV, minute=DAILY_REMINDER_MINUTE_KYIV, tzinfo=_KYIV_TZ),
        name="daily_tiktok_reminder",
    )
    logger.info(
        f"Щоденне нагадування: {DAILY_REMINDER_HOUR_KYIV:02d}:{DAILY_REMINDER_MINUTE_KYIV:02d} за Києвом."
    )

    # Нагадування "обери контент для Instagram" — раз на ~3 дні (перевірка
    # щодня о 12:00 Києва, сам job вирішує чи вже минуло 3 дні й чи є що
    # показати — див. _ig_reminder_job).
    app.job_queue.run_daily(
        _ig_reminder_job,
        time=dt_time(hour=IG_REMINDER_HOUR_KYIV, minute=IG_REMINDER_MINUTE_KYIV, tzinfo=_KYIV_TZ),
        name="ig_reminder",
    )
    logger.info(
        f"Instagram-нагадування: перевірка о {IG_REMINDER_HOUR_KYIV:02d}:{IG_REMINDER_MINUTE_KYIV:02d} "
        f"за Києвом, надсилається не частіше ніж раз на {IG_REMINDER_MIN_DAYS} дні."
    )

    logger.info("Бот запущено. Очікую відео...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
