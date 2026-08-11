"""
Telegram бот — точка входу.

Команди:
  /start       — привітання
  /status      — скільки відео опубліковано сьогодні
  /queue       — що в черзі
  /ig_pending  — TikTok-відео, ще не опубліковані в Instagram, кожне з
                 переходом до відео в чаті (reply) і кнопкою "Запостити"
  /publish_ig  — те саме одним списком, з реальними переглядами TikTok
  /nocap       — опублікувати карусель без підпису (пропустити крок підпису)

Сценарій відео:
  1. Надсилаєш відео в чат
  2. Бот обробляє: silence removal → субтитри → обкладинка → S3
  3. Бот АВТОМАТИЧНО ставить відео в чергу на TikTok (найближчий вільний
     слот з TIKTOK_PUBLISH_TIMES) — без кнопок, без підтвердження
  4. queue_runner.py публікує у заданий час (у TikTok-чернетки — публічний
     автопостинг без App Review неможливий) і надсилає в чат нагадування
     "опублікуй" з кнопкою для Instagram Reels
  5. Instagram публікується ЛИШЕ вручну — тапом кнопки "Запостити в
     Instagram" (з нагадування, /ig_pending або /publish_ig), ніколи автоматично

Сценарій сторітейл-каруселі (Instagram):
  1. Надсилаєш кілька готових слайдів ОДНИМ альбомом фото (2-10 штук)
  2. Бот вивантажує їх на S3, питає підпис (текстом або /nocap)
  3. Обираєш час публікації — queue_runner.py публікує каруселлю автоматично
"""

import asyncio
import html
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, TMP_DIR,
    TIKTOK_PUBLISH_TIMES, TIKTOK_DAILY_LIMIT, INSTAGRAM_PUBLISH_HOUR,
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
from publishers.tiktok import list_recent_public_videos as tiktok_list_recent_public_videos
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
        "👋 Привіт! Надішли відео — я його оброблю і АВТОМАТИЧНО поставлю в "
        "чергу на TikTok (найближчий вільний слот, без кнопок). Коли завантажиться "
        "в TikTok-чернетки — надішлю нагадування «опублікуй» з посиланням на "
        "відео в чаті й кнопкою, щоб одразу закинути це саме відео в Instagram Reels "
        "(TikTok все одно попросить один тап «Опублікувати» в самому застосунку — "
        "обмеження платформи, автопостинг без App Review неможливий).\n\n"
        "Якщо відео >20MB — надішли посилання:\n"
        "`/process_url https://...`\n\n"
        "Надішли кілька фото одним альбомом — запропоную опублікувати їх як "
        "сторітейл-карусель в Instagram.\n\n"
        "/status — статус сьогоднішніх публікацій\n"
        "/queue — черга\n"
        "/ig_pending — список TikTok-відео, ще не опублікованих в Instagram, "
        "кожне з переходом до відео в чаті й кнопкою «Запостити»\n"
        "/publish_ig — те саме одним списком з реальними переглядами TikTok\n"
        "/test_ig — безпечна перевірка, чи працює публікація в Instagram (нічого не публікує)\n"
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
            share_url = best.get("share_url", "")
            db.match_tiktok_public_video(cand["id"], best["id"], views, share_url)
            cand["tiktok_public_video_id"] = best["id"]
            cand["tiktok_public_views"] = views
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
    ass_paths = []
    final_video_paths = {}
    frame_path = None
    cover_path = None

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

        # 9. Автоматично плануємо TikTok (без кнопок і підтвердження) — ставить
        #    у чергу найближчий вільний слот з TIKTOK_PUBLISH_TIMES;
        #    queue_runner сам завантажить у TikTok-чернетки о цій годині і
        #    надішле сюди нагадування "опублікуй" з кнопкою для Instagram.
        scheduled_at, slot_label = _next_tiktok_slot()
        db.enqueue(video_id, "tiktok", scheduled_at)

        transcript_line = (
            f"📝 Транскрипція: <i>{html.escape(transcript[:100])}...</i>\n\n"
            if transcript and transcript.strip()
            else "📝 Мовлення не розпізнано (без субтитрів)\n\n"
        )
        await msg.edit_text(
            f"✅ Відео готове!\n\n{transcript_line}"
            f"📅 Заплановано в TikTok на {slot_label} — автоматично, без підтвердження.\n"
            "Коли завантажиться в чернетки — надішлю нагадування з кнопкою «Опублікувати в Instagram» "
            "(TikTok все одно попросить один тап «Опублікувати» в самому застосунку — "
            "це обмеження платформи, обійти в коді не можна).",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
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
        заданий у TIKTOK_PUBLISH_TIMES/INSTAGRAM_PUBLISH_HOUR/
        INSTAGRAM_REELS_HOUR, фактично зсувався б на 2-3 год від задуманого
        київського).
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


def _next_tiktok_slot() -> tuple:
    """
    Автоматично обирає найближчий вільний часовий слот для TikTok з
    TIKTOK_PUBLISH_TIMES — БЕЗ ручного вибору кнопкою (раніше тут показувалась
    клавіатура і бот чекав на тап; тепер весь крок відбувається одразу після
    обробки відео).

    Заповнює слоти дня по черзі, за порядком TIKTOK_PUBLISH_TIMES; щойно на
    сьогодні вільних слотів не лишилось (усі зайняті іншими відео, вже
    минули, або досягнуто TIKTOK_DAILY_LIMIT) — переходить на перший вільний
    слот завтра, і так далі. db.get_tiktok_queue_times() читає вже
    заплановані/опубліковані TikTok-слоти з черги, щоб два відео ніколи не
    потрапили в один і той самий час.

    Повертає (scheduled_at_utc_naive, label) — як _next_kyiv_time().
    """
    now_kyiv = datetime.now(_KYIV_TZ)
    claimed_utc = db.get_tiktok_queue_times()
    claimed_kyiv = [dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_KYIV_TZ) for dt in claimed_utc]
    max_per_day = min(len(TIKTOK_PUBLISH_TIMES), TIKTOK_DAILY_LIMIT)

    for day_offset in range(15):
        day = (now_kyiv + timedelta(days=day_offset)).date()
        day_claims = [t for t in claimed_kyiv if t.date() == day]
        if len(day_claims) >= max_per_day:
            continue
        for time_str in TIKTOK_PUBLISH_TIMES:
            h, m = map(int, time_str.split(":"))
            candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=_KYIV_TZ)
            if candidate <= now_kyiv:
                continue
            if any(abs((candidate - c).total_seconds()) < 60 for c in day_claims):
                continue  # цей слот уже зайнятий іншим відео
            scheduled_at_utc = candidate.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            if day_offset == 0:
                day_label = "сьогодні"
            elif day_offset == 1:
                day_label = "завтра"
            else:
                day_label = candidate.strftime("%d.%m")
            return scheduled_at_utc, f"{time_str} за Києвом ({day_label})"

    raise RuntimeError("Не вдалось знайти вільний TikTok-слот у найближчі 15 днів")


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
    std_path = no_silence_path = vertical_path = None
    ass_paths = []
    final_video_paths = {}
    frame_path = cover_path = None

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

        # Автоматично плануємо TikTok (без кнопок) — див. коментар у
        # _process_video_file вище.
        scheduled_at, slot_label = _next_tiktok_slot()
        db.enqueue(video_id, "tiktok", scheduled_at)

        transcript_line = f"📝 <i>{html.escape(transcript[:100])}...</i>\n\n" if transcript and transcript.strip() else ""
        await msg.edit_text(
            f"✅ «{html.escape(filename)}» готове!\n\n{transcript_line}"
            f"📅 Заплановано в TikTok на {slot_label} — автоматично.\n"
            "Надішлю нагадування з кнопкою Instagram, коли завантажиться в чернетки.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"Drive pipeline error: {e}", exc_info=True)
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
    app.add_handler(CommandHandler("ig_pending", cmd_ig_pending))
    app.add_handler(CommandHandler("test_ig", cmd_test_ig))
    app.add_handler(CommandHandler("dm_blast", cmd_dm_blast))
    app.add_handler(CommandHandler("process_url", cmd_process_url))
    app.add_handler(CommandHandler("scan_drive", cmd_scan_drive))
    app.add_handler(CallbackQueryHandler(handle_drive_process_callback, pattern=r"^drive_process:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(CallbackQueryHandler(handle_publish_ig_callback, pattern=r"^publish_ig:"))
    app.add_handler(CallbackQueryHandler(handle_dm_blast_callback, pattern=r"^dm_blast_(confirm|cancel)$"))
    app.add_handler(CommandHandler("nocap", cmd_nocap))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_carousel_photos))
    app.add_handler(CallbackQueryHandler(handle_carousel_schedule_callback, pattern=r"^schedule_carousel:"))
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

    logger.info("Бот запущено. Очікую відео...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
