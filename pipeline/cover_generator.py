"""
Генерація обкладинки для TikTok / Instagram Reels.

Два режими:
  generate_cover_ai(transcript, frame_path)  — основний:
      1. GPT-4o витягує hook-текст (коротке питання або удар) + опис сцени
      2. DALL-E 3 генерує темний атмосферний фон у стилі блогу
      3. Pillow накладає hook ЗВЕРХУ великим жирним шрифтом + логотип знизу

  generate_cover(frame_path, ...)  — fallback без AI

Шрифт пріоритети (від найкращого):
  1. assets/fonts/font.ttf  (поклади сюди Montserrat-ExtraBold.ttf або будь-який жирний)
  2. DejaVuSans-Bold / LiberationSans-Bold (системні)
  3. PIL default

Стиль DALL-E перевизначається через env COVER_STYLE_SUFFIX.
"""

import io
import json
import logging
import os
import uuid

import requests as http_requests
from PIL import Image, ImageDraw, ImageFont

from config import TMP_DIR, OPENAI_API_KEY

logger = logging.getLogger(__name__)

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
LOGO_PATH   = os.path.join(ASSETS_DIR, "logo.png")
FONT_PATH   = os.path.join(ASSETS_DIR, "fonts", "font.ttf")  # кастомний шрифт

COVER_WIDTH  = 1080
COVER_HEIGHT = 1920

# ── DALL-E стиль блогу ────────────────────────────────────────────────────────
# Орієнтований на той самий стиль що на скріншотах:
# темний фон, один сильний атмосферний об'єкт, cinematic lighting.
# Перевизначається через env COVER_STYLE_SUFFIX.
_DEFAULT_STYLE = (
    "ultra dark background (almost black with deep teal or indigo hints), "
    "one powerful central subject — could be a glowing silhouette, "
    "abstract geometric shape, glowing particle effect, cosmic element, "
    "or nature metaphor relevant to the topic. "
    "Cinematic volumetric lighting, high contrast, photorealistic digital art. "
    "No text, no watermarks, no faces, no readable words. "
    "Aspect ratio 9:16 portrait."
)
COVER_STYLE_SUFFIX = os.getenv("COVER_STYLE_SUFFIX", _DEFAULT_STYLE)


# ── Публічні функції ──────────────────────────────────────────────────────────

def generate_cover_ai(transcript: str, frame_path: str) -> str:
    """
    Генерує AI-обкладинку: DALL-E 3 фон + hook зверху + логотип.
    При помилці — fallback на кадр з відео.
    """
    if not OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY не задано — frame fallback для обкладинки")
        return generate_cover(frame_path, subtitle_text=(transcript or "")[:60])

    try:
        hook_text, dalle_prompt = _plan_cover(transcript)
        logger.info(f"Cover hook: «{hook_text}» | prompt: {dalle_prompt[:80]}…")
        bg = _dalle_generate(dalle_prompt)
        return _compose(bg, hook_text)
    except Exception as e:
        logger.warning(f"AI cover failed ({e}), using frame fallback")
        return generate_cover(frame_path, subtitle_text=(transcript or "")[:60])


def generate_cover(frame_path: str, title_text: str = "", subtitle_text: str = "") -> str:
    """Базова обкладинка з кадру відео (fallback)."""
    bg = Image.open(frame_path).convert("RGB")
    bg = _fit_cover(bg, COVER_WIDTH, COVER_HEIGHT)

    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 100))
    bg = bg.convert("RGBA")
    bg = Image.alpha_composite(bg, overlay)
    draw = ImageDraw.Draw(bg)

    if os.path.exists(LOGO_PATH):
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo = _resize_logo(logo, 220)
        bg.paste(logo, ((COVER_WIDTH - logo.width) // 2, 80), mask=logo)

    if title_text:
        _draw_text_centered(draw, title_text, _load_font(72), COVER_HEIGHT // 2 - 80, (255, 255, 255))
    if subtitle_text:
        _draw_text_centered(draw, subtitle_text, _load_font(48), COVER_HEIGHT // 2 + 20, (220, 220, 220))

    out = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_cover.jpg")
    bg.convert("RGB").save(out, "JPEG", quality=95)
    return out


# ── Внутрішні: AI pipeline ────────────────────────────────────────────────────

def _plan_cover(transcript: str) -> tuple:
    """
    GPT-4o → {"hook_text": ..., "dalle_prompt": ...}

    hook_text:   короткий удар — питання або твердження, 3-6 СЛІВ ВЕЛИКИМИ,
                 мовою транскрипції. Не заголовок лекції — а те, що змушує
                 стопнутись. Приклади: "ЩО ПІСЛЯ ГІПНОТЕРАПІЇ?",
                 "ГЕН ВІЙНИ", "МОЗОК ТЕБЕ ОБМАНЮЄ".

    dalle_prompt: англійський опис ОДНОГО сильного атмосферного об'єкту або
                  сцени (без тексту, без облич). 1-2 речення.
    """
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    system = """\
Ти генеруєш дані для обкладинки TikTok-відео про психологію та гіпнотерапію.

Поверни ТІЛЬКИ валідний JSON (без markdown, без коментарів):
{
  "hook_text": "КОРОТКА ФРАЗА АБО ПИТАННЯ 3-6 СЛІВ ВЕЛИКИМИ ЛІТЕРАМИ мовою транскрипції",
  "dalle_prompt": "English description of ONE powerful atmospheric visual: a concept, silhouette, or abstract element that represents the video topic metaphorically. No text, no faces. 1-2 sentences."
}

Правила hook_text:
- Це НЕ назва теми і НЕ заголовок лекції.
- Це емоційна зачіпка: питання, шок, або несподівана думка.
- Приклади хороших: "ЩО ПІСЛЯ ГІПНОТЕРАПІЇ?", "ГЕН ВІЙНИ", "МОЗОК ВАС ОБМАНЮЄ", "90% ЛЮДЕЙ НЕ ЗНАЮТЬ".
- Приклади поганих: "РОЗПОВІДАЮ ПРО ГІПНОЗ", "ТЕМА СЬОГОДНІ: СТРАХ".
"""

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Транскрипція:\n\n{transcript[:1200]}"},
        ],
        temperature=0.85,
        max_tokens=250,
        response_format={"type": "json_object"},
    )

    data = json.loads(resp.choices[0].message.content)
    hook   = (data.get("hook_text") or "").strip().upper()
    prompt = (data.get("dalle_prompt") or "abstract glowing silhouette on dark background").strip()
    return hook, prompt


def _dalle_generate(concept_prompt: str) -> Image.Image:
    """DALL-E 3 → PIL Image."""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    full_prompt = f"{concept_prompt}. {COVER_STYLE_SUFFIX}"

    resp = client.images.generate(
        model="dall-e-3",
        prompt=full_prompt,
        size="1024x1792",   # 9:16 portrait
        quality="standard", # ~$0.04/зображення
        n=1,
    )

    raw = http_requests.get(resp.data[0].url, timeout=45).content
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _compose(bg_image: Image.Image, hook_text: str) -> str:
    """
    Фінальна компоновка:
      - темний напівпрозорий шар зверху (~30% висоти) — щоб текст читався
      - hook ВЕЛИКИМИ зверху (там де TikTok-сітка його найкраще показує)
      - логотип внизу по центру
    """
    bg = bg_image.resize((COVER_WIDTH, COVER_HEIGHT), Image.LANCZOS).convert("RGBA")

    # Градієнт зверху (для тексту)
    top_grad = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    tg = ImageDraw.Draw(top_grad)
    grad_h = int(COVER_HEIGHT * 0.38)
    for i in range(grad_h):
        alpha = int(185 * (1 - i / grad_h))   # темніше зверху → прозоріше донизу
        tg.line([(0, i), (COVER_WIDTH, i)], fill=(0, 0, 0, alpha))
    bg = Image.alpha_composite(bg, top_grad)

    # Легке затемнення всього кадру щоб кольори не "кричали"
    dim = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 40))
    bg = Image.alpha_composite(bg, dim)

    draw = ImageDraw.Draw(bg)

    # ── Hook TEXT — зверху ────────────────────────────────────────────────────
    if hook_text:
        font_size = 104
        font = _load_font(font_size)
        lines = _wrap(hook_text, font, max_width=COVER_WIDTH - 80)
        line_h = font_size + 18
        y = 90   # відступ від верху

        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (COVER_WIDTH - tw) // 2

            # Товстий чорний stroke (робить текст читабельним на будь-якому фоні)
            stroke_w = 6
            for dx in range(-stroke_w, stroke_w + 1):
                for dy in range(-stroke_w, stroke_w + 1):
                    if abs(dx) + abs(dy) <= stroke_w + 2:
                        draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 230))
            # Білий текст поверх
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_h

    # ── Логотип — внизу по центру ─────────────────────────────────────────────
    if os.path.exists(LOGO_PATH):
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            logo = _resize_logo(logo, 180)
            lx = (COVER_WIDTH - logo.width) // 2
            ly = COVER_HEIGHT - logo.height - 80
            bg.paste(logo, (lx, ly), mask=logo)
        except Exception as e:
            logger.warning(f"Не вдалось завантажити логотип: {e}")

    out = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_cover_ai.jpg")
    bg.convert("RGB").save(out, "JPEG", quality=95)
    return out


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """
    Шукає шрифт у такому порядку:
      1. assets/fonts/font.ttf  — кастомний (наприклад Montserrat-ExtraBold)
      2. Системні жирні шрифти
      3. PIL default
    """
    candidates = [
        FONT_PATH,  # assets/fonts/font.ttf
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list:
    """Розбиває рядок на частини що вміщуються в max_width пікселів."""
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        bbox = dummy.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    ir = img.width / img.height
    tr = w / h
    if ir > tr:
        nw = int(img.width * h / img.height); nh = h
    else:
        nw = w; nh = int(img.height * w / img.width)
    img = img.resize((nw, nh), Image.LANCZOS)
    return img.crop(((nw - w) // 2, (nh - h) // 2, (nw - w) // 2 + w, (nh - h) // 2 + h))


def _resize_logo(logo: Image.Image, max_width: int) -> Image.Image:
    if logo.width > max_width:
        r = max_width / logo.width
        return logo.resize((max_width, int(logo.height * r)), Image.LANCZOS)
    return logo


def _draw_text_centered(draw, text, font, y, color):
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (COVER_WIDTH - (bbox[2] - bbox[0])) // 2
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 180))
    draw.text((x, y), text, font=font, fill=color)
