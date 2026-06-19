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

webapp/                    Сторінки Terms/Privacy + TikTok Login Kit OAuth (для TikTok App Review)
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

## TikTok: отримання access_token через Login Kit (OAuth)

Замість того щоб вручну копіювати `TIKTOK_ACCESS_TOKEN`, можна один раз пройти авторизацію
у браузері й застосунок сам збереже токен (і сам оновлюватиме його через `refresh_token`):

1. У `.env` / Railway Variables задати `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`,
   `TIKTOK_REDIRECT_URI` (= `https://<твій-railway-домен>/auth/tiktok/callback`),
   а також `ADMIN_SECRET` і `FLASK_SECRET_KEY` (будь-які довгі випадкові рядки).
2. У TikTok Developer Portal у Login Kit вказати точно той самий redirect URI.
3. Відкрити в браузері:
   `https://<твій-railway-домен>/auth/tiktok/login?key=<твій ADMIN_SECRET>`
4. Залогінитись своїм TikTok-акаунтом і підтвердити доступ — токен збережеться в БД автоматично.

`TIKTOK_ACCESS_TOKEN` / `TIKTOK_OPEN_ID` з `.env` лишаються як fallback, якщо OAuth ще не пройдено.

## Деплой на Railway

Конфігурація вже є в `railway.toml` і `Procfile`:
- сервіс `bot` — Telegram-бот, працює постійно
- сервіс `cron-checker` — щоденна перевірка найкращого відео (крон)

Усі значення з `.env` потрібно додати в Railway → Variables.

## Безпека

`.env` з реальними токенами та `*.db` файли виключені через `.gitignore` і ніколи не потрапляють у git.
