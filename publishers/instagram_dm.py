"""
Одноразова розсилка в Instagram Direct усім, хто вже писав акаунту раніше
("одноразова розсилка зараз" — НЕ автовідповідач на нові повідомлення).

Документація:
  - Conversations API: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api
  - Send Messages API: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/send-message

Потребує новий permission instagram_business_manage_messages (на додачу до
вже наявних instagram_business_basic/instagram_business_content_publish) —
після додавання scope в webapp/server.py потрібно ПЕРЕПРОЙТИ
/auth/instagram/login, стара авторизація без цього permission'у відправку
повідомлень не дозволить.

ВАЖЛИВО — обмеження платформи, які тут не можна обійти кодом:
  - 24-годинне вікно: Instagram дозволяє писати користувачу лише протягом
    24 год після його ОСТАННЬОГО повідомлення нам. Розсилка по старих
    діалогах (>24 год) для частини людей поверне помилку від API — це
    очікувано, а не баг коду. Обробляємо кожного окремо й рахуємо
    успіхи/відмови, замість падати на першій помилці.
  - Conversations API повертає тільки до 20 останніх повідомлень на діалог
    (старіші історичні повідомлення позначені як "deleted") — для визначення
    IGSID співрозмовника цього достатньо.
  - Діалоги в "Запитах" (Requests), неактивні >30 днів, API не повертає взагалі.
  - Анти-спам: уникай надсилання великої кількості ІДЕНТИЧНИХ повідомлень за
    короткий час — тримай паузу (delay_seconds) між надсиланнями, інакше Meta
    може тимчасово обмежити можливість надсилання повідомлень з акаунта.
  - Юридично: автоматизовані/бот-повідомлення мають бути позначені як такі
    (особливо для користувачів з Каліфорнії/Німеччини) — додай це в текст
    розсилки самостійно.
"""

import time
from datetime import datetime, timezone

import requests

import db

GRAPH_URL = "https://graph.instagram.com/v23.0"


def _get(url: str, access_token: str, params: dict = None) -> dict:
    params = dict(params or {})
    params["access_token"] = access_token
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def list_conversations(access_token: str, ig_user_id: str) -> list:
    """Усі діалоги акаунта, які повертає Conversations API (див. обмеження вище)."""
    conversations = []
    url = f"{GRAPH_URL}/{ig_user_id}/conversations"
    params = {"platform": "instagram", "fields": "id,updated_time"}

    while url:
        data = _get(url, access_token, params)
        conversations.extend(data.get("data", []))
        url = (data.get("paging") or {}).get("next")
        params = None  # next-посилання вже містить усі потрібні параметри

    return conversations


def _recipient_igsid(conversation_id: str, access_token: str, ig_user_id: str):
    """Визначає IGSID співрозмовника (не нашого акаунта) з останнього
    повідомлення діалогу."""
    data = _get(
        f"{GRAPH_URL}/{conversation_id}",
        access_token,
        {"fields": "messages.limit(1){from,to}"},
    )
    messages = ((data.get("messages") or {}).get("data")) or []
    if not messages:
        return None

    msg = messages[0]
    from_id = (msg.get("from") or {}).get("id")
    if from_id and str(from_id) != str(ig_user_id):
        return str(from_id)

    to_field = msg.get("to") or {}
    to_users = to_field.get("data") if isinstance(to_field, dict) else None
    for u in to_users or []:
        uid = u.get("id")
        if uid and str(uid) != str(ig_user_id):
            return str(uid)

    return None


def _is_within_24h(updated_time) -> bool:
    if not updated_time:
        return False
    try:
        raw = str(updated_time)
        # updated_time приходить як unix-секунди або unix-мілісекунди залежно від поля.
        ts = int(raw)
        if len(raw) > 10:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() < 24 * 3600


def list_dm_candidates(access_token: str, ig_user_id: str) -> list:
    """
    Повертає список {igsid, conversation_id, updated_time, within_24h} —
    усіх, хто вже мав переписку з акаунтом (за даними Conversations API).
    """
    candidates = []
    for conv in list_conversations(access_token, ig_user_id):
        igsid = _recipient_igsid(conv["id"], access_token, ig_user_id)
        if not igsid:
            continue
        updated_time = conv.get("updated_time")
        candidates.append({
            "igsid": igsid,
            "conversation_id": conv["id"],
            "updated_time": updated_time,
            "within_24h": _is_within_24h(updated_time),
        })
    return candidates


def send_dm(access_token: str, ig_user_id: str, igsid: str, text: str) -> dict:
    resp = requests.post(
        f"{GRAPH_URL}/{ig_user_id}/messages",
        params={"access_token": access_token},
        json={"recipient": {"id": igsid}, "message": {"text": text}},
        timeout=20,
    )
    data = resp.json()
    if resp.status_code >= 400 or "error" in data:
        raise RuntimeError(str(data.get("error", data)))
    return data


def run_broadcast(
    access_token: str,
    ig_user_id: str,
    text: str,
    dry_run: bool = True,
    delay_seconds: float = 4.0,
) -> dict:
    """
    Одноразова розсилка всім, хто вже писав у Direct.

    dry_run=True  — тільки рахує кандидатів, нічого не надсилає.
    dry_run=False — реально надсилає з паузою delay_seconds між повідомленнями
                    (анти-спам) і пропуском тих, кому вже надсилали раніше
                    успішно (db.instagram_dm_log) — захист від дублів при
                    повторному запуску.
    """
    candidates = list_dm_candidates(access_token, ig_user_id)
    already_sent = db.get_dmed_igsids() if not dry_run else set()

    result = {
        "total_candidates": len(candidates),
        "candidates": candidates,
        "sent": [],
        "skipped_already_sent": [],
        "failed": [],
    }

    if dry_run:
        return result

    for c in candidates:
        igsid = c["igsid"]
        if igsid in already_sent:
            result["skipped_already_sent"].append(igsid)
            continue
        try:
            send_dm(access_token, ig_user_id, igsid, text)
            db.log_dm_sent(igsid, "sent")
            result["sent"].append(igsid)
        except Exception as e:
            db.log_dm_sent(igsid, "failed", str(e))
            result["failed"].append({"igsid": igsid, "error": str(e)})
        time.sleep(delay_seconds)

    return result
