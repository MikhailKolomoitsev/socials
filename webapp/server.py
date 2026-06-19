"""
Лёгкий Flask-сервер: віддає /, /terms, /privacy.

Потрібен виключно для того, щоб мати реальні публічні URL для
TikTok App Review (Terms of Service URL / Privacy Policy URL).
Запускається у фоновому потоці поряд з Telegram-ботом (main.py),
слухає на $PORT, який Railway підставляє автоматично.
"""

import os
import threading
from datetime import date

from flask import Flask, render_template

app = Flask(__name__)

UPDATED = date.today().isoformat()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/terms")
def terms():
    return render_template("terms.html", updated=UPDATED)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", updated=UPDATED)


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
