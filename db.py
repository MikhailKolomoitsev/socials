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

            CREATE TABLE IF NOT EXISTS carousels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_urls TEXT NOT NULL,      -- JSON-масив публічних S3 URL, у порядку слайдів
                caption TEXT,
                created_at TEXT DEFAULT (datetime('now')),

                instagram_media_id TEXT,
                instagram_published_at TEXT
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
                video_id INTEGER,              -- NULL для платформи 'instagram_carousel'
                carousel_id INTEGER,           -- NULL для 'tiktok' / 'instagram'
                platform TEXT NOT NULL,        -- 'tiktok' / 'instagram' / 'instagram_carousel'
                scheduled_at TEXT NOT NULL,    -- ISO datetime
                status TEXT DEFAULT 'pending', -- pending / done / failed
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (video_id) REFERENCES videos(id),
                FOREIGN KEY (carousel_id) REFERENCES carousels(id)
            );

            CREATE TABLE IF NOT EXISTS instagram_dm_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                igsid TEXT NOT NULL,
                status TEXT NOT NULL,   -- 'sent' / 'failed'
                error TEXT,
                sent_at TEXT DEFAULT (datetime('now'))
            );
        """)
        _migrate(conn)


def _migrate(conn):
    """
    Guarded ALTER TABLE для колонок, доданих ПІСЛЯ першого деплою.
    CREATE TABLE IF NOT EXISTS вище не чіпає вже існуючі таблиці — тому нові
    колонки на старих БД (Railway volume, що переживає редеплої) додаємо тут,
    перевіряючи PRAGMA table_info, щоб не впасти на "duplicate column".
    """
    def add_column_if_missing(table: str, column: str, ddl: str):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # Реальні перегляди TikTok, зіставлені з опублікованим вручну відео
    # (див. publishers/tiktok.py:list_recent_public_videos + main.py:cmd_publish_ig)
    add_column_if_missing("videos", "tiktok_public_video_id", "tiktok_public_video_id TEXT")
    add_column_if_missing("videos", "tiktok_public_views", "tiktok_public_views INTEGER")
    add_column_if_missing("videos", "tiktok_public_share_url", "tiktok_public_share_url TEXT")

    # instagram_carousel підтримка в черзі публікацій
    add_column_if_missing("publish_queue", "carousel_id", "carousel_id INTEGER")

    # Окреме відео (інший стиль субтитрів) для Instagram — щоб платформи не
    # розпізнавали TikTok- і Instagram-версію як дублікат одна одної і не
    # різали охоплення. s3_url лишається TikTok-варіантом (як і раніше).
    add_column_if_missing("videos", "s3_url_instagram", "s3_url_instagram TEXT")

    # publish_queue.video_id був NOT NULL у старій схемі — записи для
    # instagram_carousel мають video_id=NULL (замість цього carousel_id).
    # SQLite не підтримує ALTER COLUMN DROP NOT NULL, тож на старій БД
    # (Railway volume, що переживає редеплої) перебудовуємо таблицю один раз.
    pq_info = list(conn.execute("PRAGMA table_info(publish_queue)"))
    video_id_col = next((r for r in pq_info if r[1] == "video_id"), None)
    if video_id_col and video_id_col[3] == 1:  # notnull-прапорець
        conn.executescript("""
            CREATE TABLE publish_queue_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER,
                carousel_id INTEGER,
                platform TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (video_id) REFERENCES videos(id),
                FOREIGN KEY (carousel_id) REFERENCES carousels(id)
            );
            INSERT INTO publish_queue_new (id, video_id, carousel_id, platform, scheduled_at, status, created_at)
                SELECT id, video_id, carousel_id, platform, scheduled_at, status, created_at FROM publish_queue;
            DROP TABLE publish_queue;
            ALTER TABLE publish_queue_new RENAME TO publish_queue;
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

def create_video(
    original_filename: str,
    s3_url: str,
    cover_s3_url: str,
    transcript: str,
    s3_url_instagram: str = None,
) -> int:
    """s3_url — відео зі стилем субтитрів "tiktok" (як і раніше).
    s3_url_instagram — те саме відео, але з іншим стилем субтитрів
    (колір/розмір/позиція), щоб Instagram не розпізнав його як дублікат
    TikTok-версії. Якщо не передано (напр. немає мовлення для субтитрів) —
    NULL, і publish-код сам падає назад на s3_url."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (original_filename, s3_url, cover_s3_url, transcript, s3_url_instagram) "
            "VALUES (?,?,?,?,?)",
            (original_filename, s3_url, cover_s3_url, transcript, s3_url_instagram),
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

    Сортування: відео з відомими реальними переглядами (зіставленими через
    match_tiktok_public_video, див. main.py:cmd_publish_ig) — першими, від
    найбільшої кількості переглядів; відео без зіставлення (ще в чернетках
    або ще не знайдено серед публічних) — після, за часом.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE tiktok_video_id IS NOT NULL
              AND instagram_media_id IS NULL
            ORDER BY (tiktok_public_views IS NULL) ASC, tiktok_public_views DESC, tiktok_published_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def match_tiktok_public_video(video_id: int, public_video_id: str, views: int, share_url: str):
    """Зберігає результат зіставлення чернетки з публічним TikTok-відео
    (найближчим за часом публікації) разом з реальними переглядами —
    викликається з cmd_publish_ig перед показом списку кандидатів."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE videos
               SET tiktok_public_video_id=?, tiktok_public_views=?, tiktok_public_share_url=?
               WHERE id=?""",
            (public_video_id, views, share_url, video_id),
        )


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


def enqueue_carousel(carousel_id: int, scheduled_at: datetime):
    """Ставить готову карусель (уже завантажену в S3, див. create_carousel)
    у чергу — platform завжди 'instagram_carousel', video_id лишається NULL."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO publish_queue (carousel_id, platform, scheduled_at) VALUES (?, 'instagram_carousel', ?)",
            (carousel_id, scheduled_at.isoformat()),
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
    #
    # LEFT JOIN на обидві таблиці: tiktok/instagram-записи мають video_id
    # (carousel-поля будуть NULL), instagram_carousel-записи мають carousel_id
    # (video-поля будуть NULL) — queue_runner.py розгалужується за platform.
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT q.*,
                   v.s3_url, v.s3_url_instagram, v.cover_s3_url, v.transcript, v.tiktok_caption,
                   c.image_urls AS carousel_image_urls, c.caption AS carousel_caption
            FROM publish_queue q
            LEFT JOIN videos v ON v.id = q.video_id
            LEFT JOIN carousels c ON c.id = q.carousel_id
            WHERE q.status = 'pending'
              AND datetime(q.scheduled_at) <= datetime('now')
        """).fetchall()
    return [dict(r) for r in rows]


# ── Carousels (сторітейли) ───────────────────────────────────────────────────

def create_carousel(image_urls: list, caption: str = "") -> int:
    """image_urls: список публічних S3 URL слайдів, у порядку показу (2-10)."""
    import json
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO carousels (image_urls, caption) VALUES (?, ?)",
            (json.dumps(image_urls), caption),
        )
        return cur.lastrowid


def get_carousel_by_id(carousel_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM carousels WHERE id=?", (carousel_id,)).fetchone()
    return dict(row) if row else None


def set_carousel_caption(carousel_id: int, caption: str):
    with get_conn() as conn:
        conn.execute("UPDATE carousels SET caption=? WHERE id=?", (caption, carousel_id))


def set_carousel_published(carousel_id: int, media_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE carousels SET instagram_media_id=?, instagram_published_at=datetime('now') WHERE id=?",
            (media_id, carousel_id),
        )


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
