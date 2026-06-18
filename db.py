import sqlite3
from datetime import datetime, date
from typing import Optional

DB_PATH = "socials.db"


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

            CREATE TABLE IF NOT EXISTS publish_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL,
                platform TEXT NOT NULL,        -- 'tiktok' або 'instagram'
                scheduled_at TEXT NOT NULL,    -- ISO datetime
                status TEXT DEFAULT 'pending', -- pending / done / failed
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (video_id) REFERENCES videos(id)
            );
        """)


# ── Videos ────────────────────────────────────────────────────────────────────

def create_video(original_filename: str, s3_url: str, cover_s3_url: str, transcript: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (original_filename, s3_url, cover_s3_url, transcript) VALUES (?,?,?,?)",
            (original_filename, s3_url, cover_s3_url, transcript),
        )
        return cur.lastrowid


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
    """Повертає відео, опубліковані в TikTok вчора, без Instagram публікації."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE date(tiktok_published_at) = date('now', '-1 day')
              AND tiktok_video_id IS NOT NULL
              AND instagram_media_id IS NULL
        """).fetchall()
    return [dict(r) for r in rows]


# ── Queue ─────────────────────────────────────────────────────────────────────

def enqueue(video_id: int, platform: str, scheduled_at: datetime):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO publish_queue (video_id, platform, scheduled_at) VALUES (?,?,?)",
            (video_id, platform, scheduled_at.isoformat()),
        )


def get_pending_queue():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT q.*, v.s3_url, v.cover_s3_url, v.transcript
            FROM publish_queue q
            JOIN videos v ON v.id = q.video_id
            WHERE q.status = 'pending'
              AND q.scheduled_at <= datetime('now')
        """).fetchall()
    return [dict(r) for r in rows]


def mark_queue_done(queue_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE publish_queue SET status='done' WHERE id=?", (queue_id,))


def mark_queue_failed(queue_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE publish_queue SET status='failed' WHERE id=?", (queue_id,))


def count_tiktoks_today() -> int:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM videos
            WHERE date(tiktok_published_at) = date('now')
        """).fetchone()
    return row["cnt"]
