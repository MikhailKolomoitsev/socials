"""
Публікація YouTube Shorts через YouTube Data API v3.
Документація: https://developers.google.com/youtube/v3/guides/uploading_a_video

На відміну від TikTok (inbox-чернетки без App Review) і Instagram (ручна
кнопка), YouTube дозволяє СПРАВЖНЮ автопублікацію для особистого каналу
власника — без App Review. Тому весь виклик тут одноразовий і повністю
автоматичний (main.py:_process_video_file/_process_drive_file, одразу
після обробки), без жодної кнопки в Telegram.

YouTube Data API НЕ вміє "pull from URL" (на відміну від TikTok/Instagram) —
відео завантажується resumable upload'ом напряму з локального файлу, тому
publish_short() викликається ДО видалення тимчасових файлів (finally-блок
пайплайну), з local-шляхом, а не з S3 URL.

Потрібен scope: https://www.googleapis.com/auth/youtube.upload
"""

import logging
from datetime import datetime, timedelta

import requests

import db
from config import YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
WEBSITE_URL = "https://kolomoitsev.com/"


def get_valid_access_token() -> str:
    """
    Повертає актуальний access_token, оновлюючи через refresh_token, якщо
    протермінований (Google access_token живе лише ~1 годину).
    """
    tokens = db.get_youtube_tokens()
    if not tokens:
        raise RuntimeError(
            "Немає YouTube токена: пройди /auth/youtube/login."
        )

    expires_at = datetime.fromisoformat(tokens["expires_at"])
    if datetime.now() < expires_at - timedelta(minutes=2):
        return tokens["access_token"]

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(
            f"Не вдалось оновити YouTube токен (можливо, доступ відкликано — "
            f"потрібно пройти /auth/youtube/login заново): {data}"
        )

    db.save_youtube_tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),  # Google часто НЕ повертає новий — save_youtube_tokens лишає старий
        expires_in=data.get("expires_in", 3600),
    )
    return data["access_token"]


def publish_short(local_video_path: str, title: str, description: str = "") -> str:
    """
    Заливає локальний файл як YouTube Short (публічно, одразу).

    YouTube автоматично класифікує вертикальне відео <=3 хв як Short —
    достатньо правильного aspect ratio (уже 9:16 з пайплайну) і/або хештегу
    #Shorts в description. title обрізається до 100 символів (ліміт YouTube).

    До description ЗАВЖДИ дописується посилання на сайт (WEBSITE_URL) —
    для обох шляхів публікації (автоматичного й ручного з "📺 Неопубліковані
    Shorts"), бо додається тут, в одному спільному місці для обох викликів.

    Returns:
        YouTube video ID (https://youtube.com/shorts/<id>)
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials

    creds = Credentials(token=get_valid_access_token())
    youtube = build("youtube", "v3", credentials=creds)

    if WEBSITE_URL not in description:
        description = f"{description}\n\n🔗 {WEBSITE_URL}".strip()
    if "#shorts" not in description.lower():
        description = f"{description}\n\n#Shorts".strip()

    body = {
        "snippet": {
            "title": (title or "Short").strip()[:100],
            "description": description,
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(local_video_path, mimetype="video/mp4", resumable=True)

    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"YouTube upload: {int(status.progress() * 100)}%")

    return response["id"]
