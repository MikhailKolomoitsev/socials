"""
ЗАСТАРІЛО (деактивовано): раніше це був щоденний cron job, що сам читав
перегляди TikTok-відео і публікував найкраще в Instagram автоматично.

Це більше не працює: TikTok-відео тепер потрапляє у "чернетки" (inbox)
через publishers/tiktok.py і не публікується автоматично — власник сам
відкриває TikTok і натискає "Опублікувати". Поки відео не опубліковане
публічно, TikTok Research API не повертає для нього перегляди, тож
get_video_views() для inbox-відео завжди поверне 0.

Заміна: команда /publish_ig у Telegram-боті (main.py). Власник сам бачить,
яке відео "вибухнуло" в TikTok, і вручну вибирає його зі списку — бот
публікує вибране в Instagram Reels.

Якщо в Railway досі є Cron Service, що запускає `python -m scheduler.cron_checker`,
його можна прибрати (Railway → сервіс → Settings → Cron Schedule → видалити) —
цей файл лишений просто як no-op, щоб запуск не зламався, якщо забудеш прибрати.
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [CRON] %(message)s")
logger = logging.getLogger(__name__)


def run():
    logger.info(
        "cron_checker деактивовано: автоматична перевірка переглядів TikTok "
        "більше не підтримується (відео публікується вручну власником). "
        "Використовуй команду /publish_ig у Telegram-боті."
    )


if __name__ == "__main__":
    run()
