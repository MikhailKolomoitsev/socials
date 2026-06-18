# socials

Особистий пайплайн автоматизації відео: Telegram-бот приймає відео → вирізає тишу → генерує субтитри → накладає субтитри на відео → робить кастомну обкладинку → завантажує в S3 → публікує в TikTok (3 рази на день за розкладом) → наступного дня перевіряє перегляди й републікує найкраще відео як Instagram Reels.

## Архітектура

```
main.py                  Telegram-бот (прийом відео, команди /start /status /queue)
config.py                Завантаження конфігурації з .env
db.py                     SQLite: відео, черга публікацій

pipeline/
  ffmpeg_processor.py     Вирізання тиші, накладання субтитрів (FFmpeg)
  transcriber.py          Whisper / AssemblyAI → .srt
  cover_generator.py      Кастомна обкладинка (Pillow)
  uploader.py             Завантаження файлів у S3

publishers/
  tiktok.py                Публікація в TikTok, отримання переглядів
  instagram.py             Публікація Reels в Instagram

scheduler/
  queue_runner.py          Черга публікацій у TikTok за розкладом
  cron_checker.py           Щоденна перевірка найкращого відео → публікація в Instagram

webapp/                    Сторінки Terms of Service / Privacy Policy (для TikTok App Review)
```

## Налаштування

1. Встановити залежності (рекомендовано Miniforge, якщо немає прав адміністратора на Mac):
   ```
   pip install -r requirements.txt
   ```
2. Скопіювати `.env.example` у `.env` і заповнити всі значення (Telegram токен, OpenAI/AssemblyAI ключ, S3, TikTok, Instagram).
3. Запуск локально:
   ```
   python main.py
   ```

## Деплой на Railway

Конфігурація вже є в `railway.toml` і `Procfile`:
- сервіс `bot` — Telegram-бот, працює постійно
- сервіс `cron-checker` — щоденна перевірка найкращого відео (крон)

Усі значення з `.env` потрібно додати в Railway → Variables.

## Безпека

`.env` з реальними токенами та `*.db` файли виключені через `.gitignore` і ніколи не потрапляють у git.
