"""
Перевіряє чергу публікацій кожну хвилину і публікує за розкладом Instagram
Reels/карусель. Запускається як окремий процес поряд з Telegram ботом.

TikTok у цій черзі більше НЕ обробляється — відправка в TikTok повністю
ручна, тригериться кнопкою прямо в main.py
(handle_publish_tiktok_callback), без публікації через publish_queue.
"""

import logging
import time

import json

import db
from pipeline.caption_generator import generate_caption
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
            if item["platform"] == "instagram":
                _publish_instagram(item)
            elif item["platform"] == "instagram_carousel":
                _publish_instagram_carousel(item)
            db.mark_queue_done(item["id"])
        except Exception as e:
            logger.error(f"Помилка публікації queue #{item['id']}: {e}")
            db.mark_queue_failed(item["id"])


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


def _generate_instagram_caption(transcript: str) -> str:
    return generate_caption(transcript, platform="instagram")


if __name__ == "__main__":
    db.init_db()
    run()
