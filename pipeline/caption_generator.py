"""
Генерація підпису (caption) для TikTok / Instagram через GPT-4o.

Стиль: живий, наче написаний людиною — дотепний, щирий, іноді з природньою
"людською" помилкою або розмовним скороченням. Хук на першому рядку,
релевантні хештеги (мікс нішевих + широких), CTA в кінці.
"""

import logging

from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

# ── Системні промпти ───────────────────────────────────────────────────────────

_TIKTOK_SYSTEM = """\
Ти — SMM-менеджер, що веде TikTok-блог про гіпнотерапію та ментальне здоров'я.
Автор — живий блогер, не бренд. Пишеш підписи так, ніби тільки-но зняв відео і одразу відкрив додаток.

СТИЛЬ:
- Перший рядок — хук, що змушує стопнутись (несподіване питання, контроверсійна думка, жартівлива фраза або цифра). Без "Привіт усім" і банальщини.
- 2-3 рядки суті — коротко, образно, без води. Можна вставити легкий гумор або самоіронію.
- Один рядок CTA: підпишись / збережи якщо впізнав себе / напиши в коментарях / поділись з кимось кому треба.
- Пустий рядок, потім 15-20 хештегів через пробіл. Мікс: 5-6 вузьконішевих (конкретна тема відео) + 5-6 тематичних + 4-5 широких для охоплення.
- Мова: українська (якщо транскрипція іншою мовою — пиши тією самою).
- Довжина тексту без хештегів: 150-280 символів.
- Іноді (не завжди!) — одна природня "людська" деталь: пропущена кома, скорочення "шо" замість "що", "ок" замість "добре", три крапки посередині думки...

НЕ використовуй: корпоративний тон, слово "контент", зайві смайли (не більше 2-3 і тільки якщо доречно), загальні фрази типу "поговоримо про..." або "сьогодні я розповім".\
"""

_INSTAGRAM_SYSTEM = """\
Ти — SMM-менеджер Instagram-блогу про гіпнотерапію та ментальне здоров'я.
Пишеш підписи живо і щиро — трохи більше простору ніж TikTok, більше емпатії, але без пафосу.

СТИЛЬ:
- Перший рядок — хук (питання, влучна думка, або щось що миттєво відгукується). Має змусити натиснути "ще".
- 3-5 рядків основного тексту: глибше ніж TikTok, але без лекції. Можна поділитись особистим спостереженням або кумедним моментом з практики.
- CTA: збережи / поділись / напиши в коментарях що думаєш / відправ тому хто це зараз потребує.
- Пустий рядок, потім 20-25 хештегів. Мікс: нішеві (конкретна тема) + тематичні + широкі для охоплення.
- Мова: українська (або та, якою говорять у транскрипції).
- Довжина тексту без хештегів: 200-400 символів.
- Іноді — легка "людська" деталь: розмовна конструкція, три крапки, особисте "я", скорочення.\
"""


# ── Публічний інтерфейс ────────────────────────────────────────────────────────

def generate_caption(transcript: str, platform: str = "tiktok") -> str:
    """
    Генерує підпис для публікації на основі транскрипції відео.

    Args:
        transcript: текст транскрипції
        platform:   'tiktok' або 'instagram'

    Returns:
        Готовий caption-рядок із хештегами
    """
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY не задано — використовую базовий caption")
        return _fallback(transcript, platform)

    system = _TIKTOK_SYSTEM if platform == "tiktok" else _INSTAGRAM_SYSTEM

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        f"Транскрипція відео (до 1500 символів):\n\n"
                        f"{transcript[:1500]}"
                    ),
                },
            ],
            temperature=0.92,   # трохи вище за дефолт — менше шаблонності
            max_tokens=450,
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.warning(f"GPT-4o caption generation failed ({e}), using fallback")
        return _fallback(transcript, platform)


# ── Внутрішні утиліти ─────────────────────────────────────────────────────────

def _fallback(transcript: str, platform: str) -> str:
    """Базовий caption без GPT — на випадок недоступності OpenAI."""
    base = (transcript or "").strip()[:200] or "Новий відео"
    if platform == "instagram":
        return (
            f"{base}\n\n"
            "#hypnotherapy #hypnosis #mentalhealth #psychology #selfcare "
            "#anxiety #mindset #healing #ukraineinstagram #reels #instareels "
            "#explore #motivationalquotes #wellbeing #therapy"
        )
    return (
        f"{base}\n\n"
        "#hypnotherapy #hypnosis #mentalhealth #psychology #selfcare "
        "#anxiety #mindset #healing #fyp #foryou #tiktokua #viral "
        "#learnontiktok #wellbeing #therapy"
    )
