"""
Публікація Reels в Instagram через Instagram API with Instagram Login
(Business Login) — graph.instagram.com.
Документація: https://developers.facebook.com/docs/instagram-platform/content-publishing

Потрібні permissions: instagram_business_basic, instagram_business_content_publish
(старі instagram_basic/instagram_content_publish — deprecated 27.01.2025).

Цей флоу НЕ потребує прив'язки Facebook Page: IG_USER_ID береться напряму
з токена, отриманого через /auth/instagram/login (webapp/server.py), а не
зі статичної INSTAGRAM_BUSINESS_ACCOUNT_ID.
"""

import time
from datetime import datetime

import requests

import db
from config import (
    INSTAGRAM_ACCESS_TOKEN,
    INSTAGRAM_BUSINESS_ACCOUNT_ID,
)

GRAPH_URL = "https://graph.instagram.com/v23.0"


def _get_valid_token_and_user_id():
    """
    Повертає (access_token, ig_user_id).

    Пріоритет:
      1. Токен, отриманий через Business Login (OAuth) і збережений у БД —
         автоматично оновлюється через graph.instagram.com/refresh_access_token,
         якщо протермінований.
      2. INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_BUSINESS_ACCOUNT_ID з .env — fallback
         для ручного тестування, поки OAuth не пройдено.
    """
    tokens = db.get_instagram_tokens()
    if not tokens:
        if not INSTAGRAM_ACCESS_TOKEN or not INSTAGRAM_BUSINESS_ACCOUNT_ID:
            raise RuntimeError(
                "Немає Instagram токена: пройди /auth/instagram/login або "
                "задай INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_BUSINESS_ACCOUNT_ID."
            )
        return INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID

    expires_at = datetime.fromisoformat(tokens["expires_at"])
    if datetime.now() < expires_at:
        return tokens["access_token"], tokens["ig_user_id"]

    # Токен протермінований (або скоро протермінується) — оновлюємо.
    # Long-lived токен можна оновити, поки він валідний; якщо він вже зовсім
    # протух (>60 днів без оновлення) — доведеться пройти /auth/instagram/login заново.
    resp = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": tokens["access_token"],
        },
        timeout=15,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(
            f"Не вдалось оновити Instagram токен (можливо протух >60 днів, "
            f"потрібно пройти /auth/instagram/login заново): {data}"
        )

    db.save_instagram_tokens(
        ig_user_id=tokens["ig_user_id"],
        access_token=data["access_token"],
        expires_in=data.get("expires_in", 5184000),
    )
    return data["access_token"], tokens["ig_user_id"]


def get_valid_token_and_user_id():
    """Публічна обгортка над _get_valid_token_and_user_id() — для повторного
    використання тієї самої логіки токенів (DB + auto-refresh) з інших
    модулів (напр. publishers/instagram_dm.py), без дублювання коду."""
    return _get_valid_token_and_user_id()


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
    access_token, ig_user_id = _get_valid_token_and_user_id()
    container_id = _create_container(ig_user_id, access_token, video_url, caption, cover_url)
    _wait_for_container(container_id, access_token)
    return _publish_container(ig_user_id, access_token, container_id)


def _create_container(ig_user_id, access_token: str, video_url: str, caption: str, cover_url: str = None) -> str:
    """Крок 1: Створюємо media container."""
    params = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": access_token,
    }

    if cover_url:
        params["cover_url"] = cover_url

    resp = requests.post(
        f"{GRAPH_URL}/{ig_user_id}/media",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "id" not in data:
        raise RuntimeError(f"Instagram container error: {data}")

    return data["id"]


def _wait_for_container(container_id: str, access_token: str, max_retries: int = 15):
    """Чекає поки Instagram обробить відео.

    Можливі status_code: IN_PROGRESS, FINISHED, ERROR, EXPIRED (24 год без публікації).
    """
    for attempt in range(max_retries):
        resp = requests.get(
            f"{GRAPH_URL}/{container_id}",
            params={
                "fields": "status_code,status",
                "access_token": access_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status_code")

        if status == "FINISHED":
            return
        elif status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing failed: {data}")

        time.sleep(15)

    raise TimeoutError("Instagram container processing timed out")


def adapt_caption_for_instagram(tiktok_caption: str) -> str:
    """
    Адаптує підпис з TikTok для Instagram.
    Можна розширити генерацією через OpenAI.
    """
    caption = (tiktok_caption or "").replace("#fyp", "#reels").replace("#foryou", "#explore")
    insta_tags = "\n\n#reels #instareels #hypnotherapy"
    return caption + insta_tags


def _publish_container(ig_user_id, access_token: str, container_id: str) -> str:
    """Крок 2: Публікуємо готовий container."""
    resp = requests.post(
        f"{GRAPH_URL}/{ig_user_id}/media_publish",
        params={
            "creation_id": container_id,
            "access_token": access_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "id" not in data:
        raise RuntimeError(f"Instagram publish error: {data}")

    return data["id"]
