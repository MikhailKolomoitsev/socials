import os
import sqlite3
from datetime import datetime, date
from typing import Optional

# DB_PATH задається через env (Railway Volume mount path, напр. /data/socials.db),
# щоб база не зникала при кожному редеплої. Без env — локальний файл поруч з кодом.
DB_PATH = os.getenv("DB_PATH", "socials.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT,
                s3_url TEXT NOT NULL,
                cover_s3_url TEXT,
                transcript TEXT,
                created_at TEXT DEFAULT (datetime('now')),

                -- TikTok
                tiktok_video_id TEXT,
                tiktok_published_at TEXT,
                tiktok_caption TEXT,

                -- Instagram
                instagram_media_id TEXT,
                instagram_published_at TEXT,
                instagram_caption TEXT,

                -- Аналітика
                views_at_check INTEGER DEFAULT 0,
                best_of_day INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tiktok_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                open_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS instagram_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ig_user_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS publish_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL,
                platform TEXT NOT NULL,        -- 'tiktok' або 'instagram'
                scheduled_at TEXT NOT NULL,    -- ISO datetime
                status TEXT DEFAULT 'pending', -- pending / done / failed
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (video_id) REFERENCES videos(id)
            );

            CREATE TABLE IF NOT EXISTS instagram_dm_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                igsid TEXT NOT NULL,
                status TEXT NOT NULL,   -- 'sent' / 'failed'
                error TEXT,
                sent_at TEXT DEFAULT (datetime('now'))
            );
        """)


# ── TikTok OAuth tokens ──────────────────────────────────────────────────────

def save_tiktok_tokens(open_id: str, access_token: str, refresh_token: str, expires_in: int):
    """Зберігає (перезаписує) токени TikTok, отримані через OAuth. Один рядок — один оператор."""
    from datetime import timedelta
    expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tiktok_tokens (id, open_id, access_token, refresh_token, expires_at, updated_at)
            VALUES (1, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                open_id=excluded.open_id,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                updated_at=datetime('now')
            """,
            (open_id, access_token, refresh_token, expires_at),
        )


def get_tiktok_tokens() -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tiktok_tokens WHERE id=1").fetchone()
    return dict(row) if row else None


# ── Instagram OAuth tokens (Business Login for Instagram) ───────────────────

def save_instagram_tokens(ig_user_id: str, access_token: str, expires_in: int):
    """Зберігає (перезаписує) long-lived токен Instagram. Один рядок — один оператор."""
    from datetime import timedelta
    expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO instagram_tokens (id, ig_user_id, access_token, expires_at, updated_at)
            VALUES (1, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                ig_user_id=excluded.ig_user_id,
                access_token=excluded.access_token,
                expires_at=excluded.expires_at,
                updated_at=datetime('now')
            """,
            (ig_user_id, access_token, expires_at),
        )


def get_instagram_tokens() -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM instagram_tokens WHERE id=1").fetchone()
    return dict(row) if row else None


# ── Videos ────────────────────────────────────────────────────────────────────

def create_video(original_filename: str, s3_url: str, cover_s3_url: str, transcript: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (original_filename, s3_url, cover_s3_url, transcript) VALUES (?,?,?,?)",
            (original_filename, s3_url, cover_s3_url, transcript),
        )
        return cur.lastrowid


def set_tiktok_caption_draft(video_id: int, caption: str):
    """Зберігає попередньо згенерований підпис одразу після обробки відео,
    щоб queue_runner міг його використати без повторної генерації."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET tiktok_caption=? WHERE id=? AND tiktok_caption IS NULL",
            (caption, video_id),
        )


def set_tiktok_published(video_id: int, tiktok_video_id: str, caption: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET tiktok_video_id=?, tiktok_published_at=datetime('now'), tiktok_caption=? WHERE id=?",
            (tiktok_video_id, caption, video_id),
        )


def set_instagram_published(video_id: int, media_id: str, caption: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET instagram_media_id=?, instagram_published_at=datetime('now'), instagram_caption=? WHERE id=?",
            (media_id, caption, video_id),
        )


def update_views(video_id: int, views: int):
    with get_conn() as conn:
        conn.execute("UPDATE videos SET views_at_check=? WHERE id=?", (views, video_id))


def mark_best_of_day(video_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE videos SET best_of_day=1 WHERE id=?", (video_id,))


def get_yesterdays_tiktoks():
    """Повертає відео, опубліковані в TikTok вчора, без Instagram публікації.

    Застаріле: використовувалось автоматичним cron_checker.py, який читав
    перегляди через get_video_views(). Більше не надійне, бо TikTok-відео
    тепер публікується вручну власником (inbox-флоу), а не одразу й публічно.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE date(tiktok_published_at) = date('now', '-1 day')
              AND tiktok_video_id IS NOT NULL
              AND instagram_media_id IS NULL
        """).fetchall()
    return [dict(r) for r in rows]


def get_recent_tiktoks_for_instagram(limit: int = 10):
    """Останні відео, закинуті в TikTok (inbox) і ще не опубліковані в Instagram.

    Використовується Telegram-командою "опублікувати в Instagram": власник сам
    дивиться, яке відео "вибухнуло" в TikTok, і вибирає його зі списку тут.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE tiktok_video_id IS NOT NULL
              AND instagram_media_id IS NULL
            ORDER BY tiktok_published_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_video_by_id(video_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    return dict(row) if row else None


# ── Queue ─────────────────────────────────────────────────────────────────────

def enqueue(video_id: int, platform: str, scheduled_at: datetime):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO publish_queue (video_id, platform, scheduled_at) VALUES (?,?,?)",
            (video_id, platform, scheduled_at.isoformat()),
        )


def get_pending_queue():
    # scheduled_at зберігається через Python's datetime.isoformat() — формат
    # "2026-06-25T08:34:16.224357" (з літерою "T" і мікросекундами), тоді як
    # SQLite datetime('now') повертає "2026-06-25 08:34:25" (з пробілом, без
    # мікросекунд). Пряме текстове порівняння "<=" між ними ламається: символ
    # "T" (0x54) лексикографічно більший за пробіл (0x20), тому scheduled_at
    # завжди "більший" за datetime('now') для тієї ж дати — умова ніколи не
    # спрацьовувала, і жодне відео з черги ніколи не підхоплювалось.
    # Обгортаємо обидві сторони в datetime(...), що нормалізує формат і
    # коректно парсить ISO8601 з "T"-розділювачем.
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT q.*, v.s3_url, v.cover_s3_url, v.transcript, v.tiktok_caption
            FROM publish_queue q
            JOIN videos v ON v.id = q.video_id
            WHERE q.status = 'pending'
              AND datetime(q.scheduled_at) <= datetime('now')
        """).fetchall()
    return [dict(r) for r in rows]


def mark_queue_done(queue_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE publish_queue SET status='done' WHERE id=?", (queue_id,))


def mark_queue_failed(queue_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE publish_queue SET status='failed' WHERE id=?", (queue_id,))


# ── Instagram Direct — одноразова розсилка ──────────────────────────────────

def log_dm_sent(igsid: str, status: str, error: str = None):
    """Фіксує спробу надсилання DM (sent/failed), щоб повторний запуск
    розсилки не дублював повідомлення тим, кому вже надіслано успішно."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO instagram_dm_log (igsid, status, error) VALUES (?,?,?)",
            (igsid, status, error),
        )


def get_dmed_igsids() -> set:
    """IGSID усіх, кому вже УСПІШНО надсилали розсилку (щоб не дублювати)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT igsid FROM instagram_dm_log WHERE status='sent'"
        ).fetchall()
    return {r["igsid"] for r in rows}


def is_filename_known(filename: str) -> bool:
    """Повертає True якщо відео з такою назвою вже є в БД (вже оброблялось раніше)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM videos WHERE original_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
    return row is not None


def count_tiktoks_today() -> int:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM videos
            WHERE date(tiktok_published_at) = date('now')
        """).fetchone()
    return row["cnt"]
