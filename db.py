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
                best_of_day INTEGER DEFAULT 0,

                -- Telegram: де в чаті лежить оригінальне відео (щоб пізніше
                -- надіслати нагадування-відповідь з переходом до контенту)
                chat_id INTEGER,
                message_id INTEGER
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

            CREATE TABLE IF NOT EXISTS youtube_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
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

            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,            -- напр. 'ig_choose_reel'
                sent_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS failed_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT,
                source_type TEXT NOT NULL,     -- 'telegram' / 'drive' / 'url'
                source_ref TEXT NOT NULL,      -- file_id (telegram/drive) або URL
                error TEXT,
                chat_id INTEGER,
                message_id INTEGER,
                failed_at TEXT DEFAULT (datetime('now')),
                resolved INTEGER DEFAULT 0     -- 1 після успішного повторного запуску
            );

            CREATE TABLE IF NOT EXISTS skipped_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT NOT NULL,
                reason TEXT,                   -- напр. 'no_audio'
                skipped_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cover_style_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                generation_count INTEGER NOT NULL DEFAULT 0
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

    # Лайки/коменти публічного TikTok-відео — для рекомендацій "це залетіло,
    # постав в Instagram" (див. main.py:cmd_stats).
    add_column_if_missing("videos", "tiktok_public_likes", "tiktok_public_likes INTEGER")
    add_column_if_missing("videos", "tiktok_public_comments", "tiktok_public_comments INTEGER")

    # instagram_carousel підтримка в черзі публікацій
    add_column_if_missing("publish_queue", "carousel_id", "carousel_id INTEGER")

    # Окреме відео (інший стиль субтитрів) для Instagram — щоб платформи не
    # розпізнавали TikTok- і Instagram-версію як дублікат одна одної і не
    # різали охоплення. s3_url лишається TikTok-варіантом (як і раніше).
    add_column_if_missing("videos", "s3_url_instagram", "s3_url_instagram TEXT")

    # chat_id/message_id оригінального відео в Telegram — щоб нагадування
    # "опублікуй це відео" (queue_runner, після TikTok-завантаження) і
    # /ig_pending могли надіслати reply на оригінальне повідомлення (тап на
    # цитату = перехід до контенту в чаті).
    add_column_if_missing("videos", "chat_id", "chat_id INTEGER")
    add_column_if_missing("videos", "message_id", "message_id INTEGER")

    # YouTube Shorts — публікується повністю автоматично одразу після
    # обробки (publishers/youtube.py), на відміну від TikTok/Instagram
    # жодної кнопки немає, тож тут лише факт і час публікації.
    add_column_if_missing("videos", "youtube_video_id", "youtube_video_id TEXT")
    add_column_if_missing("videos", "youtube_published_at", "youtube_published_at TEXT")

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

    # TikTok більше не публікується за автоматичним розкладом (лише кнопкою
    # "Відправити в TikTok" вручну) — будь-які старі pending-записи в черзі
    # (заплановані попередньою версією логіки, ще до цієї зміни) скасовуємо,
    # інакше queue_runner міг би відправити їх без відома власника, щойно
    # настане їхній час. Ідемпотентно: після першого прогону рядків для
    # UPDATE вже немає, ніяких нових 'tiktok'-записів у чергу не додається.
    conn.execute("""
        UPDATE publish_queue SET status='failed'
        WHERE platform = 'tiktok' AND status = 'pending'
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


# ── YouTube OAuth tokens ──────────────────────────────────────────────────────

def save_youtube_tokens(access_token: str, refresh_token: str, expires_in: int):
    """Зберігає (перезаписує) токени YouTube. refresh_token приходить лише
    ПЕРШОГО разу (Google видає його тільки при access_type=offline+prompt=consent,
    webapp/server.py:youtube_login) — тому якщо Google не повернув новий при
    оновленні access_token (publishers/youtube.py:get_valid_access_token),
    зберігаємо старий, а не NULL (ON CONFLICT SET лишає значення без COALESCE
    інакше перезаписав би на NULL)."""
    from datetime import timedelta
    expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO youtube_tokens (id, access_token, refresh_token, expires_at, updated_at)
            VALUES (1, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=COALESCE(NULLIF(excluded.refresh_token, ''), youtube_tokens.refresh_token),
                expires_at=excluded.expires_at,
                updated_at=datetime('now')
            """,
            (access_token, refresh_token or "", expires_at),
        )


def get_youtube_tokens() -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM youtube_tokens WHERE id=1").fetchone()
    return dict(row) if row else None


# ── Videos ────────────────────────────────────────────────────────────────────

def create_video(
    original_filename: str,
    s3_url: str,
    cover_s3_url: str,
    transcript: str,
    s3_url_instagram: str = None,
    chat_id: int = None,
    message_id: int = None,
) -> int:
    """s3_url — відео зі стилем субтитрів "tiktok" (як і раніше).
    s3_url_instagram — те саме відео, але з іншим стилем субтитрів
    (колір/розмір/позиція), щоб Instagram не розпізнав його як дублікат
    TikTok-версії. Якщо не передано (напр. немає мовлення для субтитрів) —
    NULL, і publish-код сам падає назад на s3_url.
    chat_id/message_id — де в Telegram лежить оригінальне відео (щоб
    нагадування й /ig_pending могли послатись на нього як reply)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (original_filename, s3_url, cover_s3_url, transcript, s3_url_instagram, chat_id, message_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (original_filename, s3_url, cover_s3_url, transcript, s3_url_instagram, chat_id, message_id),
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


def set_youtube_published(video_id: int, youtube_video_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET youtube_video_id=?, youtube_published_at=datetime('now') WHERE id=?",
            (youtube_video_id, video_id),
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


def get_recent_tiktoks_for_instagram(limit: int = 10, offset: int = 0):
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
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count_pending_instagram_reels() -> int:
    """Скільки TikTok-відео ще НЕ опубліковано в Instagram — для періодичного
    нагадування "обери рілс" (main.py:_ig_reminder_job)."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM videos
            WHERE tiktok_video_id IS NOT NULL AND instagram_media_id IS NULL
        """).fetchone()
    return row["cnt"]


def match_tiktok_public_video(
    video_id: int, public_video_id: str, views: int, share_url: str,
    likes: int = None, comments: int = None,
):
    """Зберігає результат зіставлення чернетки з публічним TikTok-відео
    (найближчим за часом публікації) разом з реальними переглядами (і,
    якщо відомі, лайками/коментарями) — викликається з cmd_publish_ig/
    cmd_stats перед показом списку кандидатів."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE videos
               SET tiktok_public_video_id=?, tiktok_public_views=?, tiktok_public_share_url=?,
                   tiktok_public_likes=?, tiktok_public_comments=?
               WHERE id=?""",
            (public_video_id, views, share_url, likes, comments, video_id),
        )


def get_video_by_id(video_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    return dict(row) if row else None


def get_videos_older_than(days: int) -> list:
    """Відео, оброблені понад `days` днів тому — для /cleanup_old (main.py).
    Повертає всі поля (потрібні s3_url/s3_url_instagram/cover_s3_url, щоб
    видалити відповідні файли зі сховища ПЕРЕД видаленням самого запису)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE datetime(created_at) <= datetime('now', ? || ' days') "
            "ORDER BY created_at ASC",
            (f"-{days}",),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_video(video_id: int):
    """Незворотно видаляє запис відео з БД. Викликати ЛИШЕ після того, як
    відповідні файли вже прибрані зі сховища (main.py:/cleanup_old) —
    інакше файли на S3 лишаться сиротами назавжди (посилання на них
    ніде більше немає)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))


# ── Queue ─────────────────────────────────────────────────────────────────────

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
                   v.chat_id, v.message_id,
                   c.image_urls AS carousel_image_urls, c.caption AS carousel_caption
            FROM publish_queue q
            LEFT JOIN videos v ON v.id = q.video_id
            LEFT JOIN carousels c ON c.id = q.carousel_id
            WHERE q.status = 'pending'
              AND datetime(q.scheduled_at) <= datetime('now')
        """).fetchall()
    return [dict(r) for r in rows]


def get_unpublished_tiktok_videos(limit: int = 15, offset: int = 0) -> list:
    """Оброблені відео, ще НЕ відправлені в TikTok (tiktok_video_id IS NULL) —
    для кнопки "Неопубліковані тіктоки". Відправка повністю ручна (кнопка
    publish_tt:<id>, main.py:handle_publish_tiktok_callback) — жодного
    автоматичного розкладу/черги для TikTok більше немає.

    ORDER BY created_at DESC — найновіші (найімовірніше актуальні/потрібні
    просто зараз) першими, а не найстаріший бэклог з червня-липня, до якого
    інакше довелось би гортати кілька сторінок пагінації."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE tiktok_video_id IS NULL
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_sent_tiktok_videos(limit: int = 20, offset: int = 0) -> list:
    """Відео, вже відправлені в TikTok (tiktok_video_id IS NOT NULL) —
    для кнопки "Відправлені тіктоки", найновіші першими."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE tiktok_video_id IS NOT NULL
            ORDER BY tiktok_published_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_sent_instagram_videos(limit: int = 20, offset: int = 0) -> list:
    """Відео, вже опубліковані в Instagram Reels (instagram_media_id IS NOT
    NULL) — для кнопки "Опубліковані Reels" з кнопкою "Відправити повторно"
    (повторна публікація створить ДРУГИЙ пост в Instagram — не чернетка, як
    у TikTok, а реальна публікація; додано на прохання власника попри цей
    ризик дублю)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE instagram_media_id IS NOT NULL
            ORDER BY instagram_published_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_unpublished_youtube_videos(limit: int = 15, offset: int = 0) -> list:
    """Відео, ще НЕ опубліковані в YouTube Shorts (youtube_video_id IS NULL),
    але ВЖЕ відправлені в TikTok (tiktok_video_id IS NOT NULL) — за проханням
    власника показувати в цьому списку лише те, що вже підтверджено як
    контент, готовий публікуватись (TikTok-чернетка вже пішла), а не будь-яке
    щойно оброблене відео. Найчастіше сюди потрапляють відео, оброблені ДО
    підключення /auth/youtube/login, або ті, де автопублікація впала
    (main.py:_auto_publish_youtube)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM videos
            WHERE youtube_video_id IS NULL
              AND tiktok_video_id IS NOT NULL
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
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
    """Повертає True, якщо файл з такою назвою вже або оброблено (videos),
    або свідомо пропущено (skipped_videos, напр. немає звуку) — щоб Drive-
    поллер не намагався розпізнати/пропустити той самий файл знову на
    кожному наступному циклі опитування (main.py:_drive_poll_job)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM videos WHERE original_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
        if row:
            return True
        row = conn.execute(
            "SELECT id FROM skipped_videos WHERE original_filename = ? LIMIT 1",
            (filename,),
        ).fetchone()
    return row is not None


def mark_skipped(filename: str, reason: str):
    """Фіксує, що файл свідомо пропущений (не оброблявся) — див.
    is_filename_known вище: без цього Drive-поллер намагався б пропустити
    той самий файл щоразу заново, надсилаючи повідомлення в чат кожні
    DRIVE_POLL_INTERVAL секунд нескінченно."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO skipped_videos (original_filename, reason) VALUES (?, ?)",
            (filename, reason),
        )


def count_tiktoks_today() -> int:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM videos
            WHERE date(tiktok_published_at) = date('now')
        """).fetchone()
    return row["cnt"]


# ── Періодичні нагадування (стан для "раз на N днів") ────────────────────────

def get_last_reminder_at(kind: str) -> Optional[datetime]:
    """Коли востаннє РЕАЛЬНО надсилалось нагадування цього типу (не щоразу,
    коли job перевіряв умову — лише коли повідомлення дійсно пішло). Стан у
    БД, а не в job_queue-розкладі, щоб пережити редеплой без збою кадансу
    (main.py:_ig_reminder_job)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sent_at FROM reminder_log WHERE kind=? ORDER BY sent_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row["sent_at"].replace(" ", "T"))


def log_reminder_sent(kind: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO reminder_log (kind) VALUES (?)", (kind,))


# ── Каруселі, "загублені" на півдорозі ───────────────────────────────────────

def get_unscheduled_carousels(limit: int = 10) -> list:
    """Каруселі, створені (create_carousel — слайди вже на S3), але так і НЕ
    доведені до кінця: без публікації і без активного запису в publish_queue
    (власник не дообрав підпис/час, або той запис впав). Для періодичного
    Instagram-нагадування (main.py:_ig_reminder_job)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.* FROM carousels c
            WHERE c.instagram_media_id IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM publish_queue q
                WHERE q.carousel_id = c.id AND q.status = 'pending'
              )
            ORDER BY c.created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Невдала обробка відео ────────────────────────────────────────────────────

def log_failed_video(
    original_filename: str, source_type: str, source_ref: str, error: str,
    chat_id: int = None, message_id: int = None,
) -> int:
    """Фіксує провал пайплайну обробки (main.py:_process_video_file /
    _process_drive_file, except-гілка) разом з достатньою інформацією, щоб
    повторно завантажити той самий файл (source_type/source_ref) для кнопки
    "Спробувати ще раз" (handle_retry_failed_callback)."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO failed_videos
               (original_filename, source_type, source_ref, error, chat_id, message_id)
               VALUES (?,?,?,?,?,?)""",
            (original_filename, source_type, source_ref, str(error)[:500], chat_id, message_id),
        )
        return cur.lastrowid


def get_failed_videos(limit: int = 15, offset: int = 0) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM failed_videos
            WHERE resolved = 0
            ORDER BY failed_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_failed_video_by_id(failed_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM failed_videos WHERE id=?", (failed_id,)).fetchone()
    return dict(row) if row else None


def resolve_failed_video(failed_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE failed_videos SET resolved=1 WHERE id=?", (failed_id,))


# ── Обкладинки: лічильник для детермінованої ротації стилю ──────────────────

def next_cover_generation_count() -> int:
    """Атомарно інкрементує й повертає лічильник згенерованих обкладинок —
    pipeline/cover_generator.py ділить його на 3, щоб стиль (колір/настрій
    фону) міняв­ся рівно раз на 3 обкладинки, а не випадково (де той самий
    стиль міг випасти двічі поспіль). У БД, а не в пам'яті процесу — інакше
    лічильник скидався б при кожному редеплої на Railway."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO cover_style_state (id, generation_count) VALUES (1, 1)
               ON CONFLICT(id) DO UPDATE SET generation_count = generation_count + 1"""
        )
        row = conn.execute("SELECT generation_count FROM cover_style_state WHERE id=1").fetchone()
    return row["generation_count"]
