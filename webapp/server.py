"""
Лёгкий Flask-сервер: віддає /, /terms, /privacy.

Потрібен виключно для того, щоб мати реальні публічні URL для
TikTok App Review (Terms of Service URL / Privacy Policy URL).
Запускається у фоновому потоці поряд з Telegram-ботом (main.py),
слухає на $PORT, який Railway підставляє автоматично.
"""

import os
import secrets
import threading
from datetime import date
from urllib.parse import urlencode

import requests
from flask import Flask, render_template, request, redirect, session, abort

import db
from config import (
    TIKTOK_CLIENT_KEY,
    TIKTOK_CLIENT_SECRET,
    TIKTOK_REDIRECT_URI,
    INSTAGRAM_CLIENT_ID,
    INSTAGRAM_CLIENT_SECRET,
    INSTAGRAM_REDIRECT_URI,
    ADMIN_SECRET,
    FLASK_SECRET_KEY,
)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

UPDATED = date.today().isoformat()

TIKTOK_SCOPES = "user.info.basic,video.upload"

# Instagram API with Instagram Login (Business Login) — заміна старих
# instagram_basic/instagram_content_publish (deprecated 27.01.2025).
INSTAGRAM_SCOPES = "instagram_business_basic,instagram_business_content_publish"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/terms")
def terms():
    return render_template("terms.html", updated=UPDATED)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", updated=UPDATED)


# ── TikTok Login Kit (OAuth) ──────────────────────────────────────────────────
# Дозволяє оператору самостійно авторизувати застосунок зі своїм TikTok-акаунтом
# і отримати access_token/refresh_token без ручного копіювання токенів.

@app.route("/auth/tiktok/login")
def tiktok_login():
    # Захист від сторонніх: тільки оператор, що знає ADMIN_SECRET, може
    # запустити цей флоу і "прив'язати" свій TikTok-акаунт до застосунку.
    if not ADMIN_SECRET or request.args.get("key") != ADMIN_SECRET:
        abort(403)

    if not TIKTOK_CLIENT_KEY or not TIKTOK_REDIRECT_URI:
        return (
            "TIKTOK_CLIENT_KEY / TIKTOK_REDIRECT_URI не задані в змінних середовища.",
            500,
        )

    state = secrets.token_urlsafe(16)
    session["tiktok_oauth_state"] = state

    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
    }
    return redirect("https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params))


@app.route("/auth/tiktok/callback")
def tiktok_callback():
    error = request.args.get("error")
    if error:
        return (
            f"❌ TikTok відхилив авторизацію: {error} — "
            f"{request.args.get('error_description', '')}",
            400,
        )

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.pop("tiktok_oauth_state", None):
        abort(400, "Невалідний state або відсутній code — почни авторизацію знову.")

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": TIKTOK_REDIRECT_URI,
        },
        timeout=15,
    )
    data = resp.json()

    if "access_token" not in data:
        return (f"❌ Не вдалось отримати токен від TikTok: {data}", 400)

    db.save_tiktok_tokens(
        open_id=data["open_id"],
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 86400),
    )

    return (
        "✅ TikTok-акаунт підключено успішно.<br>"
        f"open_id: {data['open_id']}<br>"
        "Токен збережено, можеш закрити цю сторінку — бот тепер публікуватиме "
        "від імені цього акаунта.",
        200,
    )


# ── Instagram API with Instagram Login (Business Login, OAuth) ─────────────
# Аналог TikTok Login Kit вище: оператор сам авторизує застосунок зі своїм
# Instagram-акаунтом (Business), і ми отримуємо long-lived access_token
# (60 днів), який потім сам оновлюється через graph.instagram.com/refresh_access_token.
# Документація: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/

@app.route("/auth/instagram/login")
def instagram_login():
    if not ADMIN_SECRET or request.args.get("key") != ADMIN_SECRET:
        abort(403)

    if not INSTAGRAM_CLIENT_ID or not INSTAGRAM_REDIRECT_URI:
        return (
            "INSTAGRAM_CLIENT_ID / INSTAGRAM_REDIRECT_URI не задані в змінних середовища.",
            500,
        )

    state = secrets.token_urlsafe(16)
    session["instagram_oauth_state"] = state

    params = {
        "client_id": INSTAGRAM_CLIENT_ID,
        "redirect_uri": INSTAGRAM_REDIRECT_URI,
        "response_type": "code",
        "scope": INSTAGRAM_SCOPES,
        "state": state,
    }
    return redirect("https://www.instagram.com/oauth/authorize?" + urlencode(params))


@app.route("/auth/instagram/callback")
def instagram_callback():
    error = request.args.get("error")
    if error:
        return (
            f"❌ Instagram відхилив авторизацію: {error} — "
            f"{request.args.get('error_description', '')}",
            400,
        )

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.pop("instagram_oauth_state", None):
        abort(400, "Невалідний state або відсутній code — почни авторизацію знову.")

    # Instagram любить дописувати "#_" в кінці code, якщо його скопіювали з URL вручну —
    # тут code приходить чистим через query string, але про всяк випадок підчищаємо.
    code = code.split("#")[0]

    # Крок 1: обмінюємо authorization code на short-lived access_token.
    resp = requests.post(
        "https://api.instagram.com/oauth/access_token",
        data={
            "client_id": INSTAGRAM_CLIENT_ID,
            "client_secret": INSTAGRAM_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": INSTAGRAM_REDIRECT_URI,
            "code": code,
        },
        timeout=15,
    )
    short_data = resp.json()

    # Відповідь буває або {"data":[{...}]}, або плоским об'єктом — обробляємо обидва варіанти.
    if isinstance(short_data, dict) and "data" in short_data:
        short_data = short_data["data"][0]

    short_token = short_data.get("access_token")
    ig_user_id = short_data.get("user_id")
    if not short_token or not ig_user_id:
        return (f"❌ Не вдалось отримати short-lived токен від Instagram: {short_data}", 400)

    # Крок 2: обмінюємо short-lived токен на long-lived (60 днів).
    resp = requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": INSTAGRAM_CLIENT_SECRET,
            "access_token": short_token,
        },
        timeout=15,
    )
    long_data = resp.json()

    if "access_token" not in long_data:
        return (f"❌ Не вдалось обміняти токен на long-lived: {long_data}", 400)

    db.save_instagram_tokens(
        ig_user_id=str(ig_user_id),
        access_token=long_data["access_token"],
        expires_in=long_data.get("expires_in", 5184000),  # 60 днів за замовчуванням
    )

    return (
        "✅ Instagram-акаунт підключено успішно.<br>"
        f"ig_user_id: {ig_user_id}<br>"
        "Токен збережено (дійсний 60 днів, оновлюється автоматично) — можеш "
        "закрити цю сторінку.",
        200,
    )


# TikTok "URL prefix" domain-ownership verification (signature file).
# Файл згенерований у TikTok Developer Portal при верифікації
# https://socials-production-8407.up.railway.app/ — вміст і ім'я фіксовані,
# не редагувати без повторної верифікації в TikTok.
@app.route("/tiktok4cSW6AEjMjrLwqedFZtbDoIXo7fI36rL.txt")
def tiktok_site_verification():
    return (
        "tiktok-developers-site-verification=4cSW6AEjMjrLwqedFZtbDoIXo7fI36rL",
        200,
        {"Content-Type": "text/plain"},
    )


def run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


def start_in_background() -> threading.Thread:
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    run()
