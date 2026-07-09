.PHONY: install dev bot db check clean

# ── Встановлення залежностей ──────────────────────────────────────────────────
install:
	pip install -r requirements.txt

# ── Локальний запуск бота ─────────────────────────────────────────────────────
# Перед запуском: переконайся що Railway зупинений або використовуєш окремий тест-бот.
# Перша ніч: cp .env.local.example .env.local  та заповни DB_PATH=./socials_dev.db
dev:
	python main.py

# ── Перевірка env змінних ─────────────────────────────────────────────────────
check:
	python -c "
import os; from dotenv import load_dotenv
load_dotenv(); load_dotenv('.env.local', override=True)
required = ['TELEGRAM_BOT_TOKEN','OPENAI_API_KEY','FAL_KEY','S3_BUCKET','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY']
missing = [k for k in required if not os.getenv(k)]
ok = [k for k in required if os.getenv(k)]
print('✅ OK:', ', '.join(ok))
if missing: print('❌ Відсутні:', ', '.join(missing))
else: print('Всі обовязкові змінні задані.')
print('DB_PATH:', os.getenv('DB_PATH', './socials.db (default)'))
"

# ── SQLite shell на локальній БД ──────────────────────────────────────────────
db:
	sqlite3 $$(python -c "import os; from dotenv import load_dotenv; load_dotenv(); load_dotenv('.env.local', override=True); print(os.getenv('DB_PATH','socials.db'))")

# ── Очистити кеш і тимчасові файли ───────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	rm -rf /tmp/socials
