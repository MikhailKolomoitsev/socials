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
    ADMIN_SECRET,
    FLASK_SECRET_KEY,
)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

UPDATED = date.today().isoformat()

TIKTOK_SCOPES = "user.info.basic,video.upload"


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
