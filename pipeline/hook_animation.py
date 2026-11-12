"""
Анімований "схема-хук" у перші секунди відео.

Картка з заголовком і 2-4 пунктами-чеклістом (теза відео), що по черзі
з'являються — додатковий хук поверх спікера, поки глядач вирішує, чи гортати
далі. Лише перші HOOK_DURATION секунд; далі відео йде як звичайно.

Як працює:
  1. GPT читає транскрипт і повертає заголовок + пункти (_plan_hook) —
     ТІЛЬКИ те, що спікер справді каже, мовою транскрипції.
  2. Pillow малює покадрово (HOOK_FPS) лише область картки (не весь кадр
     1080×1920 — так у рази швидше) у PNG з прозорістю.
  3. ffmpeg_processor.burn_subtitles накладає цю послідовність кадрів у тому
     ж проході, що й субтитри (без зайвого перекодування).

Будь-яка помилка тут НЕ валить пайплайн: build_hook_overlay повертає None, і
відео обробляється як раніше, без хука.
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
HOOK_DURATION = 5.0          # секунд від початку відео
CARD_W = 940
CARD_X = (1080 - CARD_W) // 2
CARD_Y = 200                 # відступ зверху (вище за субтитри, які внизу)
PAD = 44
SLIDE = 40                   # на скільки пікселів картка "виїжджає" знизу

GOLD = (212, 175, 55)
CARD_BG = (6, 12, 34, 215)
WHITE = (255, 255, 255)

TITLE_SIZE = 44
ITEM_SIZE = 46
BOX = 58                     # розмір чекбокса
ROW_GAP = 30

# Таймлайн (секунди)
CARD_IN_START, CARD_IN_END = 0.2, 0.7
ITEM_FIRST, ITEM_STEP, ITEM_ANIM = 0.9, 0.7, 0.35
FADE_OUT_START = HOOK_DURATION - 0.5


# ── GPT: план хука ────────────────────────────────────────────────────────────

_PLAN_SYSTEM = """\
Ти робиш короткий анімований "хук-схему" для початку вертикального відео
(психологія, гіпнотерапія, особистісний ріст). Це картка з заголовком і
2-4 пунктами-чеклістом, що з'являються в перші секунди.

Поверни ТІЛЬКИ валідний JSON (без markdown):
{"title": "...", "items": ["...", "..."]}

Правила:
- Мова — та сама, що в транскрипції.
- title: 2-5 слів ВЕЛИКИМИ літерами, ярлик-тема ("ГОЛОВНЕ З ВІДЕО", "ЧОМУ ТАК
  ТРАПЛЯЄТЬСЯ", "3 КРОКИ" тощо — підбери під зміст). Без клікбейтної брехні.
- items: 3 пункти (2, якщо відео дуже коротке; максимум 4). Кожен 2-6 слів,
  стисла теза, що справді звучить у відео. НІЧОГО не вигадуй і не додавай від
  себе — лише те, що сказав спікер. Без крапок у кінці.
"""


def _plan_hook(transcript: str):
    """Повертає (title, items) або None, якщо плану не вийшло."""
    if not OPENAI_API_KEY:
        return None

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": f"Транскрипція:\n\n{transcript[:3000]}"},
        ],
        temperature=0.4,
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    title = (data.get("title") or "").strip().upper()
    items = [str(i).strip().rstrip(".") for i in (data.get("items") or []) if str(i).strip()]
    items = items[:4]
    if not title or len(items) < 2:
        return None
    return title, items


# ── Рендер ────────────────────────────────────────────────────────────────────

def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _scale_alpha(img: Image.Image, factor: float) -> Image.Image:
    if factor >= 0.999:
        return img
    a = img.getchannel("A").point(lambda v: int(v * factor))
    out = img.copy()
    out.putalpha(a)
    return out


class _Layout:
    """Розкладка картки: висоти рядків, позиції — рахується один раз."""

    def __init__(self, title: str, items: list):
        self.title_font = _load_font(TITLE_SIZE)
        self.item_font = _load_font(ITEM_SIZE)
        self.title_lines = _wrap(title, self.title_font, CARD_W - 2 * PAD)
        text_w = CARD_W - 2 * PAD - BOX - 28
        self.items = [_wrap(t, self.item_font, text_w) for t in items]

        self.title_h = len(self.title_lines) * (TITLE_SIZE + 12)
        self.row_h = [max(BOX, len(lines) * (ITEM_SIZE + 10)) for lines in self.items]
        self.card_h = (
            PAD + self.title_h + 26
            + sum(self.row_h) + ROW_GAP * (len(self.items) - 1)
            + PAD
        )

    def row_y(self, i: int) -> int:
        y = PAD + self.title_h + 26
        for k in range(i):
            y += self.row_h[k] + ROW_GAP
        return y


def _card_base(layout: _Layout) -> Image.Image:
    """Фон картки (напівпрозорий прямокутник із золотою рамкою) — малюється
    у 2× і зменшується, щоб краї були згладжені (Pillow не вміє AA для фігур)."""
    s = 2
    big = Image.new("RGBA", (CARD_W * s, layout.card_h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle(
        [0, 0, CARD_W * s - 1, layout.card_h * s - 1],
        radius=36 * s, fill=CARD_BG, outline=GOLD + (255,), width=4 * s,
    )
    return big.resize((CARD_W, layout.card_h), Image.LANCZOS)


def _draw_item(layout: _Layout, i: int) -> Image.Image:
    """Один пункт (чекбокс + галочка + текст) на прозорому тлі."""
    w = CARD_W - 2 * PAD
    h = layout.row_h[i]
    s = 2
    # Чекбокс із галочкою — у 2× для згладжування
    box = Image.new("RGBA", (BOX * s, BOX * s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(box)
    bd.rounded_rectangle([3 * s, 3 * s, (BOX - 3) * s, (BOX - 3) * s],
                         radius=10 * s, outline=GOLD + (255,), width=5 * s)
    pts = [(BOX * 0.24 * s, BOX * 0.54 * s), (BOX * 0.43 * s, BOX * 0.72 * s), (BOX * 0.78 * s, BOX * 0.30 * s)]
    bd.line(pts, fill=GOLD + (255,), width=7 * s, joint="curve")
    box = box.resize((BOX, BOX), Image.LANCZOS)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.alpha_composite(box, (0, 0))
    d = ImageDraw.Draw(img)
    y = 0
    for line in layout.items[i]:
        d.text((BOX + 28, y), line, font=layout.item_font, fill=WHITE + (255,))
        y += ITEM_SIZE + 10
    return img


def render_frame(layout: _Layout, base: Image.Image, item_imgs: list, title_img: Image.Image, t: float) -> Image.Image:
    """Кадр у момент t (секунди). Розмір — (CARD_W, card_h + SLIDE)."""
    canvas = Image.new("RGBA", (CARD_W, layout.card_h + SLIDE), (0, 0, 0, 0))

    card_p = _ease_out((t - CARD_IN_START) / (CARD_IN_END - CARD_IN_START))
    if card_p <= 0 or t >= HOOK_DURATION:
        return canvas

    card = base.copy()
    card.alpha_composite(title_img, (0, 0))

    for i, item in enumerate(item_imgs):
        p = _ease_out((t - (ITEM_FIRST + ITEM_STEP * i)) / ITEM_ANIM)
        if p <= 0:
            continue
        shifted = _scale_alpha(item, p)
        card.alpha_composite(shifted, (PAD - int(40 * (1 - p)), layout.row_y(i)))  # виїжджає зліва (зсув < PAD)

    fade_out = 1.0 if t < FADE_OUT_START else max(0.0, 1 - (t - FADE_OUT_START) / (HOOK_DURATION - FADE_OUT_START))
    card = _scale_alpha(card, card_p * fade_out)
    canvas.alpha_composite(card, (0, int(SLIDE * (1 - card_p))))
    return canvas


def _title_layer(layout: _Layout) -> Image.Image:
    img = Image.new("RGBA", (CARD_W, layout.card_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = PAD
    for line in layout.title_lines:
        d.text((PAD, y), line, font=layout.title_font, fill=GOLD + (255,))
        y += TITLE_SIZE + 12
    return img


def render_overlay(title: str, items: list, out_dir: str) -> int:
    """Рендерить PNG-послідовність f0000.png… в out_dir. Повертає кількість кадрів."""
    layout = _Layout(title, items)
    base = _card_base(layout)
    title_img = _title_layer(layout)
    item_imgs = [_draw_item(layout, i) for i in range(len(items))]

    total = int(HOOK_DURATION * HOOK_FPS)
    for n in range(total):
        frame = render_frame(layout, base, item_imgs, title_img, n / HOOK_FPS)
        frame.save(os.path.join(out_dir, f"f{n:04d}.png"), compress_level=1)
    return total


# ── Публічне API ──────────────────────────────────────────────────────────────

def build_hook_overlay(transcript: str):
    """
    Повертає dict для ffmpeg_processor.burn_subtitles(hook_overlay=...):
      {"pattern": ".../f%04d.png", "fps": 30, "x": ..., "y": ..., "dir": ...}
    або None (немає транскрипції/ключа, GPT не дав плану, помилка рендера).
    Після використання викликач ОБОВ'ЯЗКОВО чистить dir (cleanup_hook_overlay).
    """
    if not transcript or not transcript.strip():
        return None

    out_dir = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_hook")
    try:
        plan = _plan_hook(transcript)
        if not plan:
            return None
        title, items = plan
        logger.info(f"Hook: «{title}» | {items}")
        os.makedirs(out_dir, exist_ok=True)
        render_overlay(title, items, out_dir)
        return {
            "pattern": os.path.join(out_dir, "f%04d.png"),
            "fps": HOOK_FPS, "x": CARD_X, "y": CARD_Y, "dir": out_dir,
        }
    except Exception as e:
        logger.warning(f"Hook-анімацію пропущено ({e})", exc_info=True)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None


def cleanup_hook_overlay(overlay) -> None:
    if overlay:
        shutil.rmtree(overlay["dir"], ignore_errors=True)
