"""
Публікація відео в TikTok через Content Posting API v2.
Документація: https://developers.tiktok.com/doc/content-posting-api-get-started

Потрібні scopes: video.upload, video.publish
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
    Публікує відео в TikTok через URL (server-side posting).

    Args:
        video_url: публічний URL відео (S3)
        caption: підпис до відео (макс. 2200 символів)
        cover_image_url: публічний URL обкладинки (опційно)

    Returns:
        publish_id (TikTok video ID після публікації)
    """
    headers = {
        "Authorization": f"Bearer {get_valid_access_token()}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    body = {
        "post_info": {
            "title": caption[:2200],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        },
    }

    if cover_image_url:
        body["post_info"]["cover_image_url"] = cover_image_url

    resp = requests.post(
        f"{BASE_URL}/post/publish/video/init/",
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
    """Чекає поки TikTok обробить відео і повертає video_id."""
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
            return data["data"].get("publicaly_available_post_id", [publish_id])[0]
        elif status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"TikTok publish failed: {data}")

        time.sleep(10)

    raise TimeoutError("TikTok publish timed out")
