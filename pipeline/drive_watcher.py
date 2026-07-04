"""
Google Drive інтеграція.

Дозволяє завантажувати відео з конкретної папки Drive без ліміту Telegram 20MB.

Workflow:
  1. Користувач закидає відео у папку TikTok Videos на Google Drive
  2. Бот кожні 5 хвилин перевіряє папку на нові файли (/scan_drive або авто)
  3. Бот завантажує файл напряму через Service Account і обробляє пайплайном

Налаштування:
  GOOGLE_SA_JSON       — JSON service account (компактний рядок)
  GOOGLE_DRIVE_FOLDER_ID — ID папки Google Drive (з URL: /folders/<ID>)
"""

import io
import json
import logging
import os
import uuid

from config import TMP_DIR, GOOGLE_SA_JSON, GOOGLE_DRIVE_FOLDER_ID

logger = logging.getLogger(__name__)

# ID файлів, які вже оброблено (щоб не дублювати між перевірками).
# При перезапуску сервісу скидається — але БД videos захистить від повторної
# публікації, бо пайплайн перевіряє наявність s3_url.
_processed_ids: set = set()


def _build_service():
    """Створює Drive API client через service account credentials."""
    if not GOOGLE_SA_JSON:
        raise RuntimeError("GOOGLE_SA_JSON не задано — додай у Railway Variables")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_new_videos(folder_id: str = None) -> list[dict]:
    """
    Повертає список нових mp4-файлів у папці Drive, яких ще не обробляли.

    Кожен елемент: {"id": ..., "name": ..., "size": ..., "created_time": ...}
    """
    fid = folder_id or GOOGLE_DRIVE_FOLDER_ID
    if not fid:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID не задано")

    service = _build_service()

    query = (
        f"'{fid}' in parents"
        " and mimeType contains 'video/'"
        " and trashed = false"
    )
    results = service.files().list(
        q=query,
        fields="files(id,name,size,createdTime,mimeType)",
        orderBy="createdTime desc",
        pageSize=20,
    ).execute()

    files = results.get("files", [])
    new_files = [f for f in files if f["id"] not in _processed_ids]
    return new_files


def download_file(file_id: str, filename: str = None) -> str:
    """
    Завантажує файл з Drive по file_id → повертає шлях до локального mp4.
    Підтримує великі файли (streaming download).
    """
    service = _build_service()

    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id)
    local_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_{filename or 'video.mp4'}")

    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    logger.info(f"Drive: завантажено {filename or file_id} → {local_path}")
    return local_path


def mark_processed(file_id: str):
    """Позначає файл як оброблений, щоб не підхоплювати вдруге."""
    _processed_ids.add(file_id)


def extract_file_id(url: str) -> str | None:
    """
    Витягує file ID з різних форматів Google Drive посилань:
      https://drive.google.com/file/d/FILE_ID/view
      https://drive.google.com/open?id=FILE_ID
      https://docs.google.com/...?id=FILE_ID
    """
    import re
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{25,})",
        r"[?&]id=([a-zA-Z0-9_-]{25,})",
        r"/folders/([a-zA-Z0-9_-]{25,})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None
