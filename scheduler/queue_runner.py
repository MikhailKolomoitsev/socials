"""
Перевіряє чергу публікацій кожні 5 хвилин і публікує відео за розкладом.
Запускається як окремий процес поряд з Telegram ботом.
"""

import logging
import time

import json

import requests

import db
from config import TELEGRAM_BOT_TOKEN
from pipeline.caption_generator import generate_caption
from publishers.tiktok import publish_video as tiktok_publish
from publishers.instagram import publish_reel, publish_carousel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [QUEUE] %(message)s")
logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # секунди між перевірками


def run():
    logger.info("Queue runner запущено.")
    while True:
        try:
            _process_queue()
        except Exception as e:
            logger.error(f"Помилка queue runner: {e}")
        time.sleep(CHECK_INTERVAL)


def _process_queue():
    items = db.get_pending_queue()
    if not items:
        return

    for item in items:
        logger.info(f"Обробляємо queue #{item['id']}: platform={item['platform']}, video_id={item['video_id']}")
        try:
            if item["platform"] == "tiktok":
                _publish_tiktok(item)
            elif item["platform"] == "instagram":
                _publish_instagram(item)
            elif item["platform"] == "instagram_carousel":
                _publish_instagram_carousel(item)
            db.mark_queue_done(item["id"])
        except Exception as e:
            logger.error(f"Помилка публікації queue #{item['id']}: {e}")
            db.mark_queue_failed(item["id"])


def _publish_tiktok(item: dict):
    # Якщо підпис вже згенеровано одразу після обробки (і збережено в DB) —
    # використовуємо його; інакше генеруємо зараз.
    caption = item.get("tiktok_caption") or _generate_tiktok_caption(item.get("transcript", ""))
    video_id = tiktok_publish(
        video_url=item["s3_url"],
        caption=caption,
        cover_image_url=item.get("cover_s3_url"),
    )
    db.set_tiktok_published(item["video_id"], video_id, caption)
    logger.info(f"✅ TikTok опубліковано: {video_id}")
    _send_tiktok_reminder(item)


def _send_tiktok_reminder(item: dict):
    """
    Нагадування в Telegram одразу після того, як відео дійшло до TikTok-
    чернеток: "відкрий TikTok і опублікуй" + кнопка, щоб одразу закинути це
    саме відео в Instagram Reels (не чекаючи /ig_pending).

    Надсилається як reply на оригінальне повідомлення з відео (chat_id/
    message_id, збережені в videos при обробці) — тап на цитату відкриває
    сам контент у чаті.

    queue_runner працює в окремому потоці без Telegram Application/event loop
    (див. main.py:main(), threading.Thread(target=queue_runner_run)) — тому
    HTTP напряму до Bot API, а не через python-telegram-bot.
    """
    chat_id = item.get("chat_id")
    video_id = item.get("video_id")
    if not chat_id or not video_id or not TELEGRAM_BOT_TOKEN:
        return

    payload = {
        "chat_id": chat_id,
        "text": (
            "🎬 Відео завантажилось у TikTok-чернетки!\n\n"
            "Відкрий TikTok і натисни «Опублікувати».\n\n"
            "Коли будеш готовий — можна одразу закинути це відео в Instagram Reels:"
        ),
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📸 Запостити в Instagram", "callback_data": f"publish_ig:{video_id}"}
            ]]
        },
    }
    if item.get("message_id"):
        payload["reply_to_message_id"] = item["message_id"]

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200 and "reply_to_message_id" in payload:
            # Оригінальне повідомлення могло бути видалене/недоступне —
            # пробуємо ще раз без reply, аби нагадування хоч дійшло.
            payload.pop("reply_to_message_id")
            resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Не вдалось надіслати нагадування в Telegram: {resp.text}")
    except Exception as e:
        logger.warning(f"Помилка надсилання нагадування в Telegram: {e}")


def _publish_instagram(item: dict):
    caption = _generate_instagram_caption(item.get("transcript", ""))
    # s3_url_instagram — версія з іншим стилем субтитрів (не такою, як у
    # TikTok), щоб Instagram не порахував це дублікатом TikTok-публікації і
    # не порізав охоплення. Fallback на s3_url — для рядків, збережених до
    # цієї зміни (де окремого Instagram-варіанту ще не було).
    video_url = item.get("s3_url_instagram") or item["s3_url"]
    media_id = publish_reel(
        video_url=video_url,
        caption=caption,
        cover_url=item.get("cover_s3_url"),
    )
    db.set_instagram_published(item["video_id"], media_id, caption)
    logger.info(f"✅ Instagram опубліковано: {media_id}")


def _publish_instagram_carousel(item: dict):
    """Публікує сторітейл-карусель (слайди підготовлені й вивантажені заздалегідь,
    див. main.py:handle_carousel_photos) — тут лише публікація за розкладом,
    без жодної додаткової генерації, як і задумано (автопублікація за часом)."""
    image_urls = json.loads(item["carousel_image_urls"])
    caption = item.get("carousel_caption") or ""
    media_id = publish_carousel(image_urls, caption)
    db.set_carousel_published(item["carousel_id"], media_id)
    logger.info(f"✅ Instagram карусель опубліковано: {media_id}")


def _generate_tiktok_caption(transcript: str) -> str:
    return generate_caption(transcript, platform="tiktok")


def _generate_instagram_caption(transcript: str) -> str:
    return generate_caption(transcript, platform="instagram")


if __name__ == "__main__":
    db.init_db()
    run()
