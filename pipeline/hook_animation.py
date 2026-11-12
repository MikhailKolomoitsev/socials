"""
Анімовані хук-картки поверх відео: емодзі + одна коротка фраза (без підписів-ярликів).

Три хуки на відео:
  1. на самому початку — головна думка відео;
  2. приблизно на 10-й секунді — наступна важлива теза;
  3. наприкінці відео — висновок.
На картці лише сама думка, без слів "головна думка"/"важливо"/"висновок".
Кожен показується HOOK_DURATION секунд. Якщо відео коротке, зайві хуки
пропускаються (щоб картки не накладались одна на одну).

Як працює:
  1. GPT читає транскрипт із таймкодами й повертає для кожного хука емодзі
     (з фіксованого набору assets/emoji) + фразу до 7 слів — ТІЛЬКИ те, що
     спікер справді каже, мовою транскрипції.
  2. Pillow малює покадрово лише область картки (не весь кадр 1080×1920) у PNG
     з прозорістю.
  3. ffmpeg_processor.burn_subtitles накладає кожну послідовність у свій
     момент (-itsoffset) в тому ж проході, що й субтитри.

Будь-яка помилка НЕ валить пайплайн: build_hook_overlay повертає None, і відео
обробляється як раніше, без хуків.

Емодзі: Twemoji (CC-BY 4.0), див. assets/emoji/LICENSE.txt.
"""

import json
import logging
import os
import shutil
import uuid

from PIL import Image, ImageDraw

from config import TMP_DIR, OPENAI_API_KEY
from pipeline.cover_generator import _load_font, _wrap

logger = logging.getLogger(__name__)

HOOK_FPS = 30
HOOK_DURATION = 4.0           # секунд на один хук
MIDDLE_HOOK_AT = 10.0         # другий хук — приблизно на 10-й секунді
END_MARGIN = 0.5              # кінцевий хук закінчується за стільки секунд до кінця
MIN_GAP = 1.0                 # мінімальна пауза між двома хуками

CARD_W = 940
CARD_X = (1080 - CARD_W) // 2
CARD_Y = 200
SLIDE = 36                    # на скільки пікселів картка "виїжджає" знизу
PAD = 36
EMOJI_SIZE = 150
GAP = 30
TEXT_SIZE = 58

GOLD = (212, 175, 55)
CARD_BG = (6, 12, 34, 225)
WHITE = (255, 255, 255)

# Таймлайн одного хука (секунди від його початку)
CARD_IN = (0.15, 0.6)
EMOJI_POP = (0.35, 0.9)
TEXT_IN = (0.6, 1.0)
FADE_OUT_START = HOOK_DURATION - 0.5

EMOJI_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "emoji")
DEFAULT_EMOJI = "💡"
# Символ → ім'я файлу (кодпойнт Twemoji). Лише ці емодзі дозволені GPT.
EMOJI_FILES = {
    "💡": "1f4a1", "🧠": "1f9e0", "❤": "2764", "🔥": "1f525", "⚠": "26a0",
    "✅": "2705", "❌": "274c", "🎯": "1f3af", "💰": "1f4b0", "📈": "1f4c8",
    "📉": "1f4c9", "⏳": "23f3", "🤔": "1f914", "😱": "1f631", "😮": "1f62e",
    "💪": "1f4aa", "🙏": "1f64f", "✨": "2728", "🌱": "1f331", "🧘": "1f9d8",
    "🛑": "1f6d1", "⚡": "26a1", "🔑": "1f511", "🚀": "1f680", "👀": "1f440",
    "🗣": "1f5e3", "💬": "1f4ac", "🧩": "1f9e9", "🎭": "1f3ad", "🌊": "1f30a",
    "🌟": "1f31f", "💔": "1f494", "😌": "1f60c", "😰": "1f630", "🤝": "1f91d",
    "🏆": "1f3c6", "📌": "1f4cc", "❓": "2753", "❗": "2757", "👉": "1f449",
    "🎓": "1f393", "😴": "1f634", "🌀": "1f300", "🔮": "1f52e", "😡": "1f621",
    "😢": "1f622",
}


# ── Планування (GPT) ──────────────────────────────────────────────────────────

_PLAN_SYSTEM = f"""\
Ти робиш короткі анімовані "хуки" для вертикального відео (психологія,
гіпнотерапія, особистісний ріст). Кожен хук — ОДНЕ емодзі + коротка фраза.

Поверни ТІЛЬКИ валідний JSON (без markdown):
{{"hooks": [{{"emoji": "💡", "text": "..."}}, {{"emoji": "🧠", "text": "..."}}, {{"emoji": "✅", "text": "..."}}]}}

Рівно 3 хуки, по порядку:
1. ГОЛОВНА ДУМКА всього відео (з цього починається відео).
2. Важлива теза з того, що звучить приблизно на 10-й секунді і трохи далі.
3. ВИСНОВОК — те, до чого спікер підводить наприкінці.

Правила:
- Мова — та сама, що в транскрипції.
- text: максимум 7 слів (до 45 символів), без крапки в кінці, стисло й
  зачіпко. НІЧОГО не вигадуй — лише те, що сказав спікер.
- emoji: ОДНЕ, строго з цього списку, що підходить за змістом:
  {" ".join(EMOJI_FILES)}
"""


def _timed_text(word_tuples, segments, transcript: str) -> str:
    """Транскрипт із позначками часу ([0:10] текст…) — щоб GPT знав, що звучить
    приблизно на 10-й секунді. Фолбек: сегменти, далі — звичайний текст."""
    lines = []
    if word_tuples:
        bucket, bucket_start = [], None
        for word, start, _end in word_tuples:
            if bucket_start is None:
                bucket_start = start
            bucket.append(word)
            if start - bucket_start >= 5.0:
                lines.append((bucket_start, " ".join(bucket)))
                bucket, bucket_start = [], None
        if bucket:
            lines.append((bucket_start, " ".join(bucket)))
    else:
        for seg in segments or []:
            get = (lambda k: seg.get(k)) if isinstance(seg, dict) else (lambda k: getattr(seg, k, None))
            text = (get("text") or "").strip()
            if text:
                lines.append((get("start") or 0, text))
    if not lines:
        return transcript
    return "\n".join(f"[{int(t) // 60}:{int(t) % 60:02d}] {text}" for t, text in lines)


def _plan_hooks(timed_transcript: str):
    """Повертає список із до 3 dict {"emoji", "text"} або None."""
    if not OPENAI_API_KEY:
        return None

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": f"Транскрипція з таймкодами:\n\n{timed_transcript[:5000]}"},
        ],
        temperature=0.4,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    hooks = []
    for h in (data.get("hooks") or [])[:3]:
        text = str(h.get("text") or "").strip().rstrip(".")
        if text:
            hooks.append({"emoji": _normalize_emoji(str(h.get("emoji") or "")), "text": text})
    return hooks or None


def _normalize_emoji(ch: str) -> str:
    ch = ch.replace("️", "").strip()
    return ch if ch in EMOJI_FILES else DEFAULT_EMOJI


def _schedule(duration: float, n_hooks: int) -> list:
    """[(індекс_хука, початок_у_секундах)] — без накладань, зі зсувом від кінця."""
    slots = []
    prev_end = -MIN_GAP
    candidates = [(0, 0.0)]
    if n_hooks > 1:
        candidates.append((1, MIDDLE_HOOK_AT))
    if n_hooks > 2:
        candidates.append((2, duration - END_MARGIN - HOOK_DURATION))
    for idx, start in candidates:
        if start < 0 or start + HOOK_DURATION > duration:
            continue
        if start < prev_end + MIN_GAP:
            continue
        slots.append((idx, start))
        prev_end = start + HOOK_DURATION
    return slots


# ── Рендер ────────────────────────────────────────────────────────────────────

def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _ease_out_back(t: float) -> float:
    """Ease з "перельотом" (емодзі трохи вистрибує й осідає)."""
    t = max(0.0, min(1.0, t))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _progress(t: float, window: tuple) -> float:
    return (t - window[0]) / (window[1] - window[0])


def _scale_alpha(img: Image.Image, factor: float) -> Image.Image:
    if factor >= 0.999:
        return img
    a = img.getchannel("A").point(lambda v: int(v * factor))
    out = img.copy()
    out.putalpha(a)
    return out


def _load_emoji(ch: str) -> Image.Image:
    path = os.path.join(EMOJI_DIR, f"{EMOJI_FILES[ch]}.png")
    return Image.open(path).convert("RGBA").resize((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)


class _Hook:
    """Розкладка й статичні шари одного хука — рахується один раз."""

    def __init__(self, emoji: str, text: str):
        text_font = _load_font(TEXT_SIZE)
        text_w = CARD_W - 2 * PAD - EMOJI_SIZE - GAP
        lines = _wrap(text, text_font, text_w)[:3]

        line_h = TEXT_SIZE + 10
        block_h = len(lines) * line_h
        content_h = max(EMOJI_SIZE, block_h)
        self.card_h = 2 * PAD + content_h

        # Фон картки — у 2× і зменшення (згладжені краї; Pillow не вміє AA для фігур)
        s = 2
        big = Image.new("RGBA", (CARD_W * s, self.card_h * s), (0, 0, 0, 0))
        ImageDraw.Draw(big).rounded_rectangle(
            [0, 0, CARD_W * s - 1, self.card_h * s - 1],
            radius=36 * s, fill=CARD_BG, outline=GOLD + (255,), width=4 * s,
        )
        self.base = big.resize((CARD_W, self.card_h), Image.LANCZOS)

        self.emoji = _load_emoji(emoji)
        self.emoji_pos = (PAD, (self.card_h - EMOJI_SIZE) // 2)

        # Текстовий шар (лише фраза, без підписів-ярликів) на прозорому тлі
        self.text_x = PAD + EMOJI_SIZE + GAP
        self.text_y = (self.card_h - block_h) // 2
        layer = Image.new("RGBA", (CARD_W, self.card_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        y = self.text_y
        for line in lines:
            d.text((self.text_x, y), line, font=text_font, fill=WHITE + (255,))
            y += line_h
        self.text_layer = layer

    def frame(self, t: float) -> Image.Image:
        """Кадр у момент t (секунди від початку хука); розмір (CARD_W, card_h + SLIDE)."""
        canvas = Image.new("RGBA", (CARD_W, self.card_h + SLIDE), (0, 0, 0, 0))
        card_p = _ease_out(_progress(t, CARD_IN))
        if card_p <= 0 or t >= HOOK_DURATION:
            return canvas

        card = self.base.copy()

        # Емодзі: "вистрибує" зі збільшенням 0.3 → 1.0 з перельотом
        e_p = _progress(t, EMOJI_POP)
        if e_p > 0:
            k = 0.3 + 0.7 * _ease_out_back(e_p)
            size = max(1, int(EMOJI_SIZE * k))
            em = self.emoji.resize((size, size), Image.LANCZOS)
            em = _scale_alpha(em, min(1.0, e_p * 2.5))
            cx = self.emoji_pos[0] + EMOJI_SIZE // 2
            cy = self.emoji_pos[1] + EMOJI_SIZE // 2
            card.alpha_composite(em, (cx - size // 2, cy - size // 2))

        # Текст: з'являється й трохи піднімається
        t_p = _ease_out(_progress(t, TEXT_IN))
        if t_p > 0:
            shifted = Image.new("RGBA", card.size, (0, 0, 0, 0))
            shifted.alpha_composite(self.text_layer, (0, int(14 * (1 - t_p))))
            card.alpha_composite(_scale_alpha(shifted, t_p))

        fade = 1.0 if t < FADE_OUT_START else max(0.0, 1 - (t - FADE_OUT_START) / (HOOK_DURATION - FADE_OUT_START))
        card = _scale_alpha(card, card_p * fade)
        canvas.alpha_composite(card, (0, int(SLIDE * (1 - card_p))))
        return canvas


def render_hook(emoji: str, text: str, out_dir: str) -> int:
    """Рендерить PNG-послідовність f0000.png… в out_dir. Повертає кількість кадрів."""
    hook = _Hook(emoji, text)
    total = int(HOOK_DURATION * HOOK_FPS)
    for n in range(total):
        hook.frame(n / HOOK_FPS).save(os.path.join(out_dir, f"f{n:04d}.png"), compress_level=1)
    return total


# ── Публічне API ──────────────────────────────────────────────────────────────

def build_hook_overlay(transcript: str, word_tuples, segments, duration: float):
    """
    Повертає список dict для ffmpeg_processor.burn_subtitles(hook_overlay=...):
      [{"pattern": ".../f%04d.png", "fps": 30, "x": ..., "y": ..., "start": 0.0, "dir": ...}, ...]
    або None (немає транскрипції/ключа, GPT не дав плану, помилка рендера).
    Після використання викликач ОБОВ'ЯЗКОВО викликає cleanup_hook_overlay.
    """
    if not transcript or not transcript.strip():
        return None

    overlays = []
    try:
        hooks = _plan_hooks(_timed_text(word_tuples, segments, transcript))
        if not hooks:
            return None
        for idx, start in _schedule(duration, len(hooks)):
            h = hooks[idx]
            logger.info(f"Hook {idx + 1} @ {start:.1f}s: {h['emoji']} «{h['text']}»")
            out_dir = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_hook{idx + 1}")
            os.makedirs(out_dir, exist_ok=True)
            overlays.append({
                "pattern": os.path.join(out_dir, "f%04d.png"),
                "fps": HOOK_FPS, "x": CARD_X, "y": CARD_Y, "start": start, "dir": out_dir,
            })
            render_hook(h["emoji"], h["text"], out_dir)
        return overlays or None
    except Exception as e:
        logger.warning(f"Hook-анімацію пропущено ({e})", exc_info=True)
        cleanup_hook_overlay(overlays)
        return None


def cleanup_hook_overlay(overlays) -> None:
    for o in overlays or []:
        shutil.rmtree(o["dir"], ignore_errors=True)
