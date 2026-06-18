"""
Щоденний cron job (запускається о 10:00):
  1. Читає вчорашні TikTok відео
  2. Перевіряє views через TikTok API
  3. Публікує найкраще (якщо > VIEWS_THRESHOLD) як Instagram Reels

На Railway: додай Railway Cron Service з командою:
  python -m scheduler.cron_checker
"""

import logging
from datetime import datetime

import db
from config import VIEWS_THRESHOLD
from publishers.tiktok import get_video_views
from publishers.instagram import publish_reel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [CRON] %(message)s")
logger = logging.getLogger(__name__)


def run():
    logger.info("Запуск щоденної перевірки TikTok статистики...")

    videos = db.get_yesterdays_tiktoks()

    if not videos:
        logger.info("Немає вчорашніх TikTok відео для перевірки.")
        return

    # Збираємо статистику
    stats = []
    for video in videos:
        try:
            views = get_video_views(video["tiktok_video_id"])
            db.update_views(video["id"], views)
            stats.append((video, views))
            logger.info(f"Video {video['tiktok_video_id']}: {views} переглядів")
        except Exception as e:
            logger.error(f"Помилка отримання stats для video {video['id']}: {e}")

    if not stats:
        logger.warning("Не вдалось отримати статистику жодного відео.")
        return

    # Знаходимо найкраще
    best_video, best_views = max(stats, key=lambda x: x[1])
    logger.info(f"Найкраще відео: id={best_video['id']}, views={best_views}")

    if best_views < VIEWS_THRESHOLD:
        logger.info(f"Найкраще відео має {best_views} переглядів < поріг {VIEWS_THRESHOLD}. Пропускаємо.")
        return

    # Генеруємо caption для Instagram (відрізняється від TikTok)
    insta_caption = _adapt_caption_for_instagram(best_video.get("tiktok_caption", ""))

    try:
        media_id = publish_reel(
            video_url=best_video["s3_url"],
            caption=insta_caption,
            cover_url=best_video.get("cover_s3_url"),
        )
        db.set_instagram_published(best_video["id"], media_id, insta_caption)
        db.mark_best_of_day(best_video["id"])
        logger.info(f"✅ Опубліковано в Instagram: media_id={media_id}")
    except Exception as e:
        logger.error(f"❌ Помилка публікації в Instagram: {e}")
        raise


def _adapt_caption_for_instagram(tiktok_caption: str) -> str:
    """
    Адаптує підпис з TikTok для Instagram.
    Можна розширити генерацією через OpenAI.
    """
    # Замінюємо TikTok-специфічні теги на Instagram
    caption = tiktok_caption.replace("#fyp", "#reels").replace("#foryou", "#explore")

    # Додаємо Instagram-специфічні хештеги
    insta_tags = "\n\n#reels #instareels #hypnotherapy"
    return caption + insta_tags


if __name__ == "__main__":
    db.init_db()
    run()
