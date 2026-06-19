import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))

# Транскрипція
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# S3
S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION", "eu-central-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL")

# TikTok
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN")  # fallback, якщо OAuth ще не пройдено
TIKTOK_OPEN_ID = os.getenv("TIKTOK_OPEN_ID")             # fallback

# TikTok Login Kit (OAuth) — для отримання access_token самостійно, без ручного копіювання
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")  # напр. https://<railway-domain>/auth/tiktok/callback

# Секрет, що захищає /auth/tiktok/login від сторонніх — тільки оператор знає це значення
ADMIN_SECRET = os.getenv("ADMIN_SECRET")

# Ключ для підпису Flask-сесії (потрібен для state у OAuth-флоу)
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# Instagram
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_BUSINESS_ACCOUNT_ID = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")

# Логіка публікацій
TIKTOK_DAILY_LIMIT = int(os.getenv("TIKTOK_DAILY_LIMIT", "3"))
TIKTOK_PUBLISH_TIMES = os.getenv("TIKTOK_PUBLISH_TIMES", "09:00,13:00,18:00").split(",")
VIEWS_THRESHOLD = int(os.getenv("VIEWS_THRESHOLD", "500"))
INSTAGRAM_PUBLISH_HOUR = int(os.getenv("INSTAGRAM_PUBLISH_HOUR", "10"))

# Локальні папки
TMP_DIR = "/tmp/socials"
os.makedirs(TMP_DIR, exist_ok=True)
