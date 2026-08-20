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

import db
from config import TMP_DIR, OPENAI_API_KEY, FAL_KEY

logger = logging.getLogger(__name__)

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
LOGO_PATH   = os.path.join(ASSETS_DIR, "logo.png")
FONT_PATH   = os.path.join(ASSETS_DIR, "fonts", "font.ttf")  # кастомний шрифт

COVER_WIDTH  = 1080
COVER_HEIGHT = 1920

# ── DALL-E стиль блогу ────────────────────────────────────────────────────────
# Спільна база (бренд-впізнаваність: темно, кінематографічно, без тексту/облич,
# 9:16 — інакше логотип і hook-текст зверху/знизу не будуть читабельні) +
# один із варіантів кольору/настрою. Раніше обирався ВИПАДКОВО (random.choice)
# щоразу — але випадковість могла (і, судячи з фідбеку, реально випадала)
# підряд обрати той самий стиль двічі-тричі, тож "різноманітність" на око не
# відчувалась. Тепер — детермінована ротація через лічильник у БД
# (db.next_cover_generation_count): стиль ГАРАНТОВАНО міняється рівно раз на
# COVER_STYLE_ROTATE_EVERY обкладинок, по колу через усі варіанти.
# COVER_STYLE_SUFFIX (env) — явний override: якщо задано, ротація вимикається
# і завжди йде саме цей стиль (як і раніше).
_BASE_STYLE = (
    "Cinematic volumetric lighting, high contrast, photorealistic digital art. "
    "No text, no watermarks, no faces, no readable words. "
    "Aspect ratio 9:16 portrait."
)

_STYLE_VARIANTS = [
    "ultra dark background (almost black with deep teal or indigo hints), "
    "one powerful central subject — a glowing silhouette or abstract geometric shape.",

    "ultra dark background (near-black with deep crimson or burgundy undertones), "
    "one dramatic central subject bathed in a single warm red rim light.",

    "ultra dark background (charcoal black with cold cyan-blue accent light), "
    "a stark architectural or geometric structure fading into shadow.",

    "ultra dark background (black with warm amber and bronze glow), "
    "a single object wreathed in drifting smoke or embers.",

    "ultra dark background (deep violet-black with faint magenta haze), "
    "a surreal, dreamlike shape suspended in empty space.",

    "near-black monochrome background with ONE sharp accent color streak "
    "(electric green, gold, or icy blue), minimalist and stark.",

    "ultra dark background (almost black with subtle emerald-teal fog), "
    "rippling water or fractured glass catching a single light source.",

    "ultra dark background (deep indigo-black), a lone human silhouette from "
    "behind or in profile, never facing camera, backlit by a distant glow.",

    "ultra dark background (black with cold steel-blue moonlight), "
    "storm clouds, lightning, or wind-blown particles frozen mid-motion.",

    "ultra dark background (near-black with warm firelight orange glow), "
    "roots, branches, or organic tendrils reaching from the shadows.",
]


COVER_STYLE_ROTATE_EVERY = 3


def _pick_style() -> str:
    override = os.getenv("COVER_STYLE_SUFFIX")
    if override:
        return override
    count = db.next_cover_generation_count()
    index = ((count - 1) // COVER_STYLE_ROTATE_EVERY) % len(_STYLE_VARIANTS)
    return f"{_STYLE_VARIANTS[index]} {_BASE_STYLE}"


# ── Перегенерація за настроєм ("🌟 Яскравіше" / "🌑 Похмуріше" / "🎨 Незвичніше") ─
#
# На відміну від звичайної ротації (_pick_style, змінюється сама по собі раз
# на 3 обкладинки), тут напрям обирає власник вручну — тому НЕ через
# db.next_cover_generation_count() (це не мало б збивати звичайний каданс
# ротації для наступних НЕ-перегенерованих обкладинок).
MOOD_STYLES = {
    "brighter": (
        "brighter, higher-energy atmosphere — still cinematic, but with vivid saturated color "
        "(warm gold, vivid teal, hot pink, or electric orange), a strong visible light source, "
        "far less shadow than a typical dark moody shot."
    ),
    "darker": (
        "much darker and more ominous atmosphere than usual — near-total black, heavy oppressive "
        "shadow, only a single dim light source, unsettling and moody."
    ),
    "unusual": (
        "deliberately strange and unexpected visual concept — an unconventional, surreal, "
        "slightly bizarre composition that breaks from typical stock-photo framing, while "
        "staying tasteful and relevant to the topic."
    ),
}


def regenerate_cover_ai(transcript: str, mood: str) -> str:
    """
    Перегенерує обкладинку для ВЖЕ обробленого відео (кнопки "🌟/🌑/🎨" під
    обкладинкою) — нові hook_text + image_prompt від GPT (не той самий, що
    минулого разу — "незвична" мала б і сам концепт міняти, не лише колір),
    background зі зміщенням у бік mood (MOOD_STYLES) замість звичайної
    ротації стилю.

    На відміну від generate_cover_ai() тут НЕМАЄ fallback на кадр з відео —
    локальний файл кадру давно видалений (обробка вже завершилась), тож при
    помилці просто піднімає виняток.
    """
    hook_text, image_prompt = _plan_cover(transcript)
    logger.info(f"Regen cover ({mood}): «{hook_text}» | prompt: {image_prompt[:80]}…")
    style_override = f"{MOOD_STYLES[mood]} {_BASE_STYLE}"
    bg = _fal_generate(image_prompt, style_override=style_override)
    return _compose(bg, hook_text)


# ── Публічні функції ──────────────────────────────────────────────────────────

def generate_cover_ai(transcript: str, frame_path: str) -> str:
    """
    Генерує AI-обкладинку: fal.ai FLUX фон + hook зверху + логотип.
    При помилці — fallback на кадр з відео.

    Пріоритет генератора зображень:
      1. fal.ai FLUX (FAL_KEY) — висока якість, нативний 9:16, ~$0.003-0.006/зображення
      2. Fallback: кадр з відео з текстом (без AI зображення)
    """
    if not OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY не задано — frame fallback для обкладинки")
        return generate_cover(frame_path, subtitle_text=(transcript or "")[:60])

    try:
        hook_text, image_prompt = _plan_cover(transcript)
        logger.info(f"Cover hook: «{hook_text}» | prompt: {image_prompt[:80]}…")
        bg = _fal_generate(image_prompt)
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
  "dalle_prompt": "English description of ONE powerful atmospheric visual that represents the video topic metaphorically. No text, no faces. 1-2 sentences."
}

Правила hook_text:
- Це НЕ назва теми і НЕ заголовок лекції.
- Це емоційна зачіпка: питання, шок, або несподівана думка.
- Приклади хороших: "ЩО ПІСЛЯ ГІПНОТЕРАПІЇ?", "ГЕН ВІЙНИ", "МОЗОК ВАС ОБМАНЮЄ", "90% ЛЮДЕЙ НЕ ЗНАЮТЬ".
- Приклади поганих: "РОЗПОВІДАЮ ПРО ГІПНОЗ", "ТЕМА СЬОГОДНІ: СТРАХ".

Правила dalle_prompt — РІЗНОМАНІТНІСТЬ найважливіша: обери об'єкт/сцену, що
підходить САМЕ ЦІЙ темі, а не завжди одне й те саме "glowing silhouette".
Обирай з різних категорій залежно від змісту, наприклад:
- людська постать ЗІ СПИНИ або в профіль (ніколи обличчям до камери)
- побутовий предмет-символ: дзеркало, ключ, замок, годинник, маска, ланцюг
  (цілий або розірваний), двері, лабіринт, шахи
- природна стихія: вогонь/дим, вода/хвилі, коріння дерева, шторм/блискавка,
  туман
- архітектура/геометрія: сходи, коридор, розбите скло, геометричні форми
- абстракція: частинки світла, туман, дим, що складається у форму
Уникай повторювати той самий тип об'єкта, що й у попередній обкладинці (якщо
з контексту видно попередню тему) — шукай несподіваний, але доречний образ.
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


def _fal_generate(concept_prompt: str, style_override: str = None) -> Image.Image:
    """
    fal.ai FLUX → PIL Image (1080×1920, нативний 9:16).

    Модель: fal-ai/flux/dev
      - висока якість, ~$0.025/зображення
      - підтримує довільний розмір
      - час генерації: ~5-15 сек

    style_override — готовий (уже з _BASE_STYLE) рядок стилю, напр. з
    regenerate_cover_ai (mood-перегенерація) — якщо задано, звичайна
    ротація (_pick_style, і її лічильник у БД) НЕ чіпається.

    Якщо FAL_KEY не задано — піднімає RuntimeError → fallback у generate_cover_ai.
    """
    if not FAL_KEY:
        raise RuntimeError("FAL_KEY не задано — пропускаємо fal.ai генерацію")

    import fal_client

    style = style_override if style_override is not None else _pick_style()
    full_prompt = f"{concept_prompt}. {style}"

    # Встановлюємо ключ для fal-client (він читає змінну середовища FAL_KEY,
    # але встановлюємо явно для надійності)
    import os as _os
    _os.environ.setdefault("FAL_KEY", FAL_KEY)

    result = fal_client.run(
        "fal-ai/flux/dev",
        arguments={
            "prompt": full_prompt,
            "image_size": {
                "width": 1080,
                "height": 1920,
            },
            "num_inference_steps": 28,
            "guidance_scale": 3.5,
            "num_images": 1,
            "enable_safety_checker": False,
            "output_format": "jpeg",
        },
    )

    image_url = result["images"][0]["url"]
    raw = http_requests.get(image_url, timeout=45).content
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
        font_size = 83  # було 104 — за проханням ~80% від оригінального розміру
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
