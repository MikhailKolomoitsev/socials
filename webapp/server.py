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


def run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


def start_in_background() -> threading.Thread:
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    run()
