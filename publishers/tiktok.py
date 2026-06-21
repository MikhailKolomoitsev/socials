"""
Публікація відео в TikTok через Content Posting API v2.
Документація: https://developers.tiktok.com/doc/content-posting-api-get-started-upload-content

Без App Review (без аудиту) TikTok дозволяє лише "draft upload" —
відео потрапляє у вхідні (inbox) акаунта, і власник має сам відкрити
сповіщення і натиснути "Опублікувати" в самому TikTok. Це навмисний
вибір (без публічного, погодженого додатку): жодного автопостингу
від імені TikTok, тільки доставка в чернетки.

Потрібен лише scope: video.upload
"""

import time
from datetime import datetime

import requests

import db
from config import (
    TIKTOK_ACCESS_TOKEN,
    TIKTOK_CLIENT_KEY,
    TIKTOK_CLIENT_SECRET,
)

BASE_URL = "https://open.tiktokapis.com/v2"


def get_valid_access_token() -> str:
    """
    Повертає актуальний access_token.

    Пріоритет:
      1. Токен, отриманий через Login Kit (OAuth) і збережений у БД —
         автоматично оновлюється через refresh_token, якщо протермінований.
      2. TIKTOK_ACCESS_TOKEN з .env — fallback для ручного тестування,
         поки OAuth не пройдено.
    """
    tokens = db.get_tiktok_tokens()
    if not tokens:
        if not TIKTOK_ACCESS_TOKEN:
            raise RuntimeError(
                "Немає TikTok токена: пройди /auth/tiktok/login або задай TIKTOK_ACCESS_TOKEN."
            )
        return TIKTOK_ACCESS_TOKEN

    expires_at = datetime.fromisoformat(tokens["expires_at"])
    if datetime.now() < expires_at:
        return tokens["access_token"]

    # Токен протермінований — оновлюємо через refresh_token.
    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Не вдалось оновити TikTok токен: {data}")

    db.save_tiktok_tokens(
        open_id=data.get("open_id", tokens["open_id"]),
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", tokens["refresh_token"]),
        expires_in=data.get("expires_in", 86400),
    )
    return data["access_token"]


def publish_video(video_url: str, caption: str, cover_image_url: str = None) -> str:
    """
    Закидає відео в TikTok "чернетки" (inbox draft upload) через URL.

    Це НЕ автопостинг: TikTok надішле власнику акаунта сповіщення,
    і він має сам відкрити TikTok і натиснути "Опублікувати". Це
    свідомий вибір, щоб не проходити повний App Review (Direct Post
    API вимагає аудиту і не призначений для особистих/непублічних
    застосунків).

    Args:
        video_url: публічний URL відео (S3)
        caption: підпис до відео (тут не використовується TikTok'ом —
                 inbox-флоу не приймає title/опис, власник дописує
                 підпис сам у застосунку перед публікацією)
        cover_image_url: не використовується inbox-флоу (залишено
                 для сумісності викликів)

    Returns:
        publish_id (ідентифікатор завдання на завантаження в TikTok)
    """
    headers = {
        "Authorization": f"Bearer {get_valid_access_token()}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    body = {
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        },
    }

    resp = requests.post(
        f"{BASE_URL}/post/publish/inbox/video/init/",
        headers=headers,
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"TikTok publish error: {data}")

    publish_id = data["data"]["publish_id"]
    return _wait_for_publish(publish_id, headers)


def get_video_views(video_id: str) -> int:
    """
    Читає кількість переглядів відео через Research API.

    Args:
        video_id: TikTok video ID

    Returns:
        Кількість переглядів
    """
    headers = {
        "Authorization": f"Bearer {get_valid_access_token()}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        f"{BASE_URL}/video/query/",
        headers=headers,
        json={
            "filters": {"video_ids": [video_id]},
            "fields": "id,view_count,like_count,share_count",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    videos = data.get("data", {}).get("videos", [])
    if not videos:
        return 0

    return videos[0].get("view_count", 0)


def _wait_for_publish(publish_id: str, headers: dict, max_retries: int = 10) -> str:
    """
    Чекає поки TikTok обробить відео і завантажить його у "чернетки".

    Для inbox-флоу (без App Review) PUBLISH_COMPLETE означає лише, що
    відео успішно дійшло до вхідних TikTok-акаунта — НЕ що його вже
    опубліковано. "publicaly_available_post_id" тут відсутній, бо
    публікацію довершує сам власник вручну в застосунку TikTok.
    """
    for attempt in range(max_retries):
        resp = requests.post(
            f"{BASE_URL}/post/publish/status/fetch/",
            headers=headers,
            json={"publish_id": publish_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("data", {}).get("status")

        if status == "PUBLISH_COMPLETE":
            post_ids = data["data"].get("publicaly_available_post_id")
            return post_ids[0] if post_ids else publish_id
        elif status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"TikTok publish failed: {data}")

        time.sleep(10)

    raise TimeoutError("TikTok publish timed out")
