"""
Завантаження файлів на S3 / Cloudflare R2 (сумісний з S3 API).
"""

import os
import uuid
import boto3
from botocore.exceptions import ClientError
from config import S3_BUCKET, S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_PUBLIC_BASE_URL


def get_s3_client():
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )


def upload_file(local_path: str, prefix: str = "videos") -> str:
    """
    Завантажує файл на S3 і повертає публічний URL.

    Args:
        local_path: локальний шлях до файлу
        prefix: папка в бакеті (videos / covers)

    Returns:
        Публічний URL файлу
    """
    ext = os.path.splitext(local_path)[1]
    key = f"{prefix}/{uuid.uuid4().hex}{ext}"

    content_type = _get_content_type(ext)

    s3 = get_s3_client()
    s3.upload_file(
        local_path,
        S3_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
    )

    return f"{S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"


def _get_content_type(ext: str) -> str:
    types = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".srt": "text/plain",
    }
    return types.get(ext.lower(), "application/octet-stream")
