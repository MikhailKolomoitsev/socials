"""
Автоматичне видалення відеофайлів з S3/R2 після публікації в Instagram Reels.

Reels — останній крок життя відео (TikTok-чернетка і YouTube вже пройшли
раніше), тож після успішної публікації файли більше не потрібні, а місце в
бакеті коштує грошей і тільки росте. Видаляємо ЛИШЕ відеофайли
(s3_url і s3_url_instagram). Обкладинка (кілька сотень КБ) і сам запис у БД
лишаються — на них спираються статистика та історія публікацій.

Помилка видалення НІКОЛИ не валить публікацію: файл просто лишиться, а
наступна спроба — /cleanup_published.
"""

import logging

import db
from pipeline.uploader import delete_file

logger = logging.getLogger(__name__)


def delete_video_files(video: dict) -> tuple[int, list]:
    """Видаляє з бакета s3_url та s3_url_instagram цього відео.

    Повертає (скільки файлів видалено, список помилок). files_deleted_at
    у БД ставиться лише коли помилок немає — інакше /cleanup_published
    пізніше спробує ще раз (delete_object ідемпотентний).
    """
    urls = {video.get("s3_url"), video.get("s3_url_instagram")}
    deleted = 0
    errors = []
    for url in urls:
        if not url:
            continue
        try:
            delete_file(url)
            deleted += 1
        except Exception as e:
            logger.warning(f"Не вдалось видалити {url} з S3: {e}")
            errors.append(f"#{video.get('id')}: {e}")
    if not errors:
        db.mark_files_deleted(video["id"])
    return deleted, errors


def cleanup_after_reel(video_id: int) -> None:
    """Викликається одразу після db.set_instagram_published(). Ніколи не
    піднімає виняток."""
    try:
        video = db.get_video_by_id(video_id)
        if not video or video.get("files_deleted_at"):
            return
        deleted, errors = delete_video_files(video)
        if deleted and not errors:
            logger.info(f"🗑 Видалено {deleted} файл(ів) відео #{video_id} з S3 після публікації в Reels")
    except Exception as e:
        logger.warning(f"Автовидалення файлів відео #{video_id} не вдалось: {e}")
