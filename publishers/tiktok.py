"""
Публікація відео в TikTok через Content Posting API v2.
Документація: https://developers.tiktok.com/doc/content-posting-api-get-started

Потрібні scopes: video.upload, video.publish
"""

import time
import requests
from config import TIKTOK_ACCESS_TOKEN, TIKTOK_OPEN_ID

BASE_URL = "https://open.tiktokapis.com/v2"


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
        "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
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
        "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
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
