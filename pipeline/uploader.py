"""
Завантаження файлів на S3 / Cloudflare R2 (сумісний з S3 API).
"""

import os
import uuid
import boto3
from botocore.exceptions import ClientError
from config import (
    S3_BUCKET,
    S3_REGION,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    S3_PUBLIC_BASE_URL,
    S3_ENDPOINT_URL,
)


def get_s3_client():
    kwargs = dict(
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    if S3_ENDPOINT_URL:
        # Cloudflare R2 (S3-сумісний API)
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
    return boto3.client("s3", **kwargs)


def upload_file(local_path: str, prefix: str = "videos") -> str:
    """
    Завантажує файл на S3/R2 і повертає публічний URL.

    Args:
        local_path: локальний шлях до файлу
        prefix: папка в бакеті (videos / covers)

    Returns:
        Публічний URL файлу
    """
    ext = os.path.splitext(local_path)[1]
    key = f"{prefix}/{uuid.uuid4().hex}{ext}"

    content_type = _get_content_type(ext)

    extra_args = {"ContentType": content_type}
    if not S3_ENDPOINT_URL:
        # R2 не підтримує object ACL — публічність вмикається на рівні бакета
        # (Public Development URL / власний домен), а не через ACL.
        extra_args["ACL"] = "public-read"

    s3 = get_s3_client()
    s3.upload_file(
        local_path,
        S3_BUCKET,
        key,
        ExtraArgs=extra_args,
    )

    return f"{S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"


def delete_file(url: str):
    """
    Видаляє файл з S3/R2 за його публічним URL (той самий S3_PUBLIC_BASE_URL-
    префікс, що й upload_file повертає — тут просто відрізаємо його, щоб
    дістати key). Використовується /cleanup_old (main.py) для видалення
    старих відео/обкладинок разом з їхнім записом у БД.

    Мовчки повертається, якщо url порожній — виклик з циклу над кількома
    URL відео (не всі поля завжди заповнені, напр. s3_url_instagram може
    дублювати s3_url) не має падати на None.
    """
    if not url:
        return
    base = S3_PUBLIC_BASE_URL.rstrip("/")
    if not url.startswith(base + "/"):
        raise ValueError(f"URL не належить S3_PUBLIC_BASE_URL ({base}): {url}")
    key = url[len(base) + 1:]

    s3 = get_s3_client()
    s3.delete_object(Bucket=S3_BUCKET, Key=key)


def key_from_url(url: str):
    """S3-ключ за публічним URL; None, якщо URL не з нашого бакета."""
    if not url:
        return None
    base = S3_PUBLIC_BASE_URL.rstrip("/")
    return url[len(base) + 1:] if url.startswith(base + "/") else None


def list_all_keys() -> set:
    """Усі ключі бакета одним проходом (list_objects_v2 з пагінацією)."""
    keys = set()
    paginator = get_s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def public_url(key: str) -> str:
    return f"{S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"


def presign_put(key: str, content_type: str, expires: int = 3600) -> str:
    """Підписаний URL для прямого PUT-завантаження з телефона в сховище —
    великі відео не проходять через наш сервер."""
    return get_s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def download_key(key: str, dest_path: str) -> str:
    get_s3_client().download_file(S3_BUCKET, key, dest_path)
    return dest_path


def delete_key(key: str) -> None:
    get_s3_client().delete_object(Bucket=S3_BUCKET, Key=key)


def _get_content_type(ext: str) -> str:
    types = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".srt": "text/plain",
    }
    return types.get(ext.lower(), "application/octet-stream")
