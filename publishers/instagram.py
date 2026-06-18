"""
Публікація Reels в Instagram через Graph API.
Документація: https://developers.facebook.com/docs/instagram-api/guides/reels

Потрібні permissions: instagram_basic, instagram_content_publish
"""

import time
import requests
from config import INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID

GRAPH_URL = "https://graph.facebook.com/v19.0"


def publish_reel(video_url: str, caption: str, cover_url: str = None) -> str:
    """
    Публікує Reels в Instagram (двоетапний процес: create → publish).

    Args:
        video_url: публічний URL відео (S3)
        caption: підпис
        cover_url: обкладинка (S3 URL, 1080×1920)

    Returns:
        Instagram media ID
    """
    container_id = _create_container(video_url, caption, cover_url)
    _wait_for_container(container_id)
    return _publish_container(container_id)


def _create_container(video_url: str, caption: str, cover_url: str = None) -> str:
    """Крок 1: Створюємо media container."""
    params = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    }

    if cover_url:
        params["cover_url"] = cover_url

    resp = requests.post(
        f"{GRAPH_URL}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "id" not in data:
        raise RuntimeError(f"Instagram container error: {data}")

    return data["id"]


def _wait_for_container(container_id: str, max_retries: int = 15):
    """Чекає поки Instagram обробить відео."""
    for attempt in range(max_retries):
        resp = requests.get(
            f"{GRAPH_URL}/{container_id}",
            params={
                "fields": "status_code,status",
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status_code")

        if status == "FINISHED":
            return
        elif status == "ERROR":
            raise RuntimeError(f"Instagram processing failed: {data}")

        time.sleep(15)

    raise TimeoutError("Instagram container processing timed out")


def _publish_container(container_id: str) -> str:
    """Крок 2: Публікуємо готовий container."""
    resp = requests.post(
        f"{GRAPH_URL}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media_publish",
        params={
            "creation_id": container_id,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "id" not in data:
        raise RuntimeError(f"Instagram publish error: {data}")

    return data["id"]
