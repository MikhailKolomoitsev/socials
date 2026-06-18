"""
Перевіряє чергу публікацій кожні 5 хвилин і публікує відео за розкладом.
Запускається як окремий процес поряд з Telegram ботом.
"""

import logging
import time

import db
from publishers.tiktok import publish_video as tiktok_publish
from publishers.instagram import publish_reel

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
            db.mark_queue_done(item["id"])
        except Exception as e:
            logger.error(f"Помилка публікації queue #{item['id']}: {e}")
            db.mark_queue_failed(item["id"])


def _publish_tiktok(item: dict):
    caption = _generate_tiktok_caption(item.get("transcript", ""))
    video_id = tiktok_publish(
        video_url=item["s3_url"],
        caption=caption,
        cover_image_url=item.get("cover_s3_url"),
    )
    db.set_tiktok_published(item["video_id"], video_id, caption)
    logger.info(f"✅ TikTok опубліковано: {video_id}")


def _publish_instagram(item: dict):
    caption = _generate_instagram_caption(item.get("transcript", ""))
    media_id = publish_reel(
        video_url=item["s3_url"],
        caption=caption,
        cover_url=item.get("cover_s3_url"),
    )
    db.set_instagram_published(item["video_id"], media_id, caption)
    logger.info(f"✅ Instagram опубліковано: {media_id}")


def _generate_tiktok_caption(transcript: str) -> str:
    """Генерує caption. Розшир через OpenAI якщо потрібно."""
    base = transcript[:200] if transcript else "Новий контент"
    return f"{base}\n\n#hypnotherapy #hypnosis #mentalhealth #fyp #foryou"


def _generate_instagram_caption(transcript: str) -> str:
    base = transcript[:200] if transcript else "Новий контент"
    return f"{base}\n\n#hypnotherapy #hypnosis #mentalhealth #reels #instareels"


if __name__ == "__main__":
    db.init_db()
    run()
