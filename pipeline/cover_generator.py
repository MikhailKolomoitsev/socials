"""
Генерація обкладинки для TikTok / Instagram Reels.

Режими (COVER_MODE у env: random — за замовчуванням, або frame / photo / flux):
  generate_cover_ai(transcript, frame_path)  — "frame" ("подкаст-стиль"):
      кадр із відео (ти в кадрі) + плашка-тег зліва зверху, золотий
      підзаголовок з рискою, великий заголовок (біле + золоте) знизу на
      темному градієнті. Текст — інформативна зачіпка від GPT-4o
      (pipeline/cover_generator.py:_plan_cover_text, _compose_frame).
      Якщо кадру/ключа немає чи щось падає — спрацьовує старий шлях нижче.

  Старий режим "flux" / регенерація за настроєм:
      1. GPT-4o витягує hook-текст (коротке питання або удар) + опис сцени
      2. fal.ai FLUX генерує теплий, світлий, шамансько-балійський
         медитативний фон (природа/ритуальні предмети — НЕ темний/похмурий,
         контент сам по собі світлий, обкладинка має притягувати)
      3. Pillow накладає hook ЗВЕРХУ великим жирним шрифтом + логотип знизу

  generate_cover(frame_path, ...)  — fallback без AI

Шрифт пріоритети (від найкращого):
  1. assets/fonts/font.ttf  (поклади сюди Montserrat-ExtraBold.ttf або будь-який жирний)
  2. DejaVuSans-Bold / LiberationSans-Bold (системні)
  3. PIL default

Стиль fal.ai перевизначається через env COVER_STYLE_SUFFIX.
"""

import io
import json
import logging
import os
import random
import uuid

import requests as http_requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import db
from config import TMP_DIR, OPENAI_API_KEY, FAL_KEY

logger = logging.getLogger(__name__)

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
LOGO_PATH   = os.path.join(ASSETS_DIR, "logo.png")
FONT_PATH   = os.path.join(ASSETS_DIR, "fonts", "font.ttf")  # кастомний шрифт

COVER_WIDTH  = 1080
COVER_HEIGHT = 1920

# ── Стиль обкладинки ──────────────────────────────────────────────────────────
# ДО (не робити знову): кожен варіант починався з "ultra dark background
# (almost black...)" — тобто попри 10 "різних" варіантів усі однаково
# похмуро-чорно-бірюзові, а контент (психологія/гіпнотерапія) сам по собі
# світлий і заспокійливий — обкладинка відштовхувала замість притягувати.
# ТЕПЕР: тепла, світла, шамансько-балійська медитативна естетика — природа,
# сонячне світло, предмети для медитації/ритуалу. Спільна база (бренд-
# впізнаваність: м'яке природне світло, без тексту/облич, 9:16 — інакше
# логотип і hook-текст зверху/знизу не будуть читабельні) + один із варіантів
# настрою/сцени. Детермінована ротація через лічильник у БД
# (db.next_cover_generation_count): стиль ГАРАНТОВАНО міняється рівно раз на
# COVER_STYLE_ROTATE_EVERY обкладинок, по колу через усі варіанти — без
# ризику випадково обрати те саме двічі поспіль.
# COVER_STYLE_SUFFIX (env) — явний override: якщо задано, ротація вимикається
# і завжди йде саме цей стиль.
_BASE_STYLE = (
    "Soft warm natural light, golden-hour glow, photorealistic, serene and "
    "inviting atmosphere — never dark, gloomy, or ominous. "
    "No text, no watermarks, no faces, no readable words. "
    "Aspect ratio 9:16 portrait."
)

_STYLE_VARIANTS = [
    "warm golden sunlight filtering through tropical palm leaves, soft dappled "
    "light, lush green and amber tones, peaceful jungle atmosphere.",

    "a Balinese stone temple offering (canang sari) with flower petals and "
    "gently curling incense smoke, warm morning sunlight, soft blurred background.",

    "dew drops on a lotus leaf resting on calm turquoise water, gentle morning "
    "mist, soft warm light, tranquil close-up.",

    "incense smoke curling upward through a warm sunbeam, a simple wooden bowl "
    "and lit candle nearby, cozy meditative still life.",

    "sunrise over misty green mountains, warm pink-and-orange sky, silhouettes "
    "of palm trees at the edge of frame, no people.",

    "smooth river stones and natural crystals arranged on raw light wood, soft "
    "warm glow, minimalist meditation altar.",

    "a single feather drifting through warm backlit sunlight, soft golden "
    "bokeh, gentle and airy.",

    "sunlight rays filtering through a bamboo forest, warm green and gold "
    "tones, calm and grounding.",

    "ocean waves at golden hour, warm amber light reflecting on the water, "
    "wide and calming horizon.",

    "a softly glowing brass singing bowl with rippling water inside, warm "
    "candlelight around it, serene close-up.",
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
        "even brighter and more vibrant — vivid saturated warm color (bright gold, coral, "
        "turquoise, or sunny orange), strong direct sunlight, high energy and joyful."
    ),
    "darker": (
        "deeper, quieter twilight atmosphere — still warm and serene (not ominous or scary), "
        "richer amber/indigo dusk tones, more shadow and depth, calm and grounding rather than gloomy."
    ),
    "unusual": (
        "deliberately dreamlike and surreal — a mystical, symbolic shamanic image (mandala "
        "patterns, double-exposure nature imagery, spiritual symbolism), unexpected but still "
        "warm, tasteful, and relevant to the topic."
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
    _hook, image_prompt = _plan_cover(transcript)
    plan = _plan_cover_text(transcript)
    logger.info(f"Regen cover ({mood}): {plan} | prompt: {image_prompt[:80]}…")
    style_override = f"{MOOD_STYLES[mood]} {_BASE_STYLE}"
    bg = _fal_generate(image_prompt, style_override=style_override)
    return _compose_cover(bg.resize((COVER_WIDTH, COVER_HEIGHT), Image.LANCZOS), plan)


# ── Публічні функції ──────────────────────────────────────────────────────────

def _pick_source(has_frame: bool) -> str:
    """Звідки фон обкладинки: "frame" (кадр відео) / "photo" (твоє фото з
    assets/photos) / "flux" (генерація fal.ai). COVER_MODE=random (за
    замовчуванням) обирає навмання; можна примусово frame|photo|flux."""
    mode = os.getenv("COVER_MODE", "random").lower()
    if mode in ("frame", "photo", "flux"):
        return mode if (mode != "frame" or has_frame) else "photo"
    options = [("photo", 35), ("flux", 35)] + ([("frame", 30)] if has_frame else [])
    return random.choices([o for o, _ in options], weights=[w for _, w in options])[0]


def generate_cover_ai(transcript: str, frame_path: str) -> str:
    """
    Генерує обкладинку в "езотеричному подкаст-стилі": випадковий фон (кадр із
    відео / твоє фото / FLUX) + тег, підзаголовок і градієнтний заголовок
    (_plan_cover_text, _compose_cover). Будь-яка помилка → наступний варіант
    фону, а зрештою — базова обкладинка з кадру.
    """
    if not OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY не задано — frame fallback для обкладинки")
        return generate_cover(frame_path, subtitle_text=(transcript or "")[:60])

    has_frame = bool(frame_path and os.path.exists(frame_path))
    source = _pick_source(has_frame)
    try:
        plan = _plan_cover_text(transcript)
        logger.info(f"Cover [{source}]: {plan}")
        if source == "flux":
            try:
                _hook, image_prompt = _plan_cover(transcript)
                bg = _fal_generate(image_prompt)
            except Exception as e:
                logger.warning(f"FLUX-фон не вдався ({e}), беру фото")
                source = "photo"
        if source == "frame":
            bg = _fit_cover(Image.open(frame_path).convert("RGB"), COVER_WIDTH, COVER_HEIGHT)
        if source == "photo":
            bg = _random_photo_background()
        elif source == "flux":
            bg = bg.resize((COVER_WIDTH, COVER_HEIGHT), Image.LANCZOS)
        return _compose_cover(bg, plan)
    except Exception as e:
        logger.warning(f"AI cover failed ({e}), using frame fallback", exc_info=True)
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


def add_part_label(cover_path: str, part_number: int, total_parts: int) -> str:
    """
    Домальовує позначку "ЧАСТИНА N/M" поверх УЖЕ готової обкладинки — для
    відео, розрізаних на частини через задовгу тривалість (main.py, поріг
    ~1.5 хв). AI-генерація (GPT hook + fal.ai фон) відбувається ОДИН РАЗ на
    все вихідне відео (дорого й повільно) — сюди приходить вже готовий
    cover_path, і ця функція лише додає napис на копію, без повторної
    генерації.

    Бейдж — у верхньому правому куті (hook_text по центру зверху, логотип
    знизу — кут завжди вільний), напівпрозора чорна плашка з білим текстом.
    """
    img = Image.open(cover_path).convert("RGBA")
    draw = ImageDraw.Draw(img)

    text = f"ЧАСТИНА {part_number}/{total_parts}"
    font = _load_font(44)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    pad_x, pad_y, margin = 28, 16, 40
    box_w, box_h = tw + pad_x * 2, th + pad_y * 2
    x0, y0 = COVER_WIDTH - box_w - margin, margin
    x1, y1 = x0 + box_w, y0 + box_h

    badge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(badge).rounded_rectangle([x0, y0, x1, y1], radius=box_h // 2, fill=(0, 0, 0, 190))
    img = Image.alpha_composite(img, badge)

    draw = ImageDraw.Draw(img)
    draw.text((x0 + pad_x - bbox[0], y0 + pad_y - bbox[1]), text, font=font, fill=(255, 255, 255, 255))

    out = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_cover_part{part_number}.jpg")
    img.convert("RGB").save(out, "JPEG", quality=95)
    return out


# ── Внутрішні: AI pipeline ────────────────────────────────────────────────────

def _plan_cover(transcript: str) -> tuple:
    """
    GPT-4o → {"hook_text": ..., "dalle_prompt": ...}

    hook_text:   короткий удар — питання або твердження, 3-6 СЛІВ ВЕЛИКИМИ,
                 мовою транскрипції. Не заголовок лекції — а те, що змушує
                 стопнутись. Приклади: "ЩО ПІСЛЯ ГІПНОТЕРАПІЇ?",
                 "ГЕН ВІЙНИ", "МОЗОК ТЕБЕ ОБМАНЮЄ".

    dalle_prompt: англійський опис ОДНОГО теплого, світлого, медитативного
                  об'єкту або сцени (без тексту, без облич). 1-2 речення.
    """
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    system = """\
Ти генеруєш дані для обкладинки TikTok-відео про психологію та гіпнотерапію.

Поверни ТІЛЬКИ валідний JSON (без markdown, без коментарів):
{
  "hook_text": "КОРОТКА ФРАЗА АБО ПИТАННЯ 3-6 СЛІВ ВЕЛИКИМИ ЛІТЕРАМИ мовою транскрипції",
  "dalle_prompt": "English description of ONE warm, bright, meditative visual that represents the video topic metaphorically. No text, no faces. 1-2 sentences."
}

Правила hook_text:
- Це НЕ назва теми і НЕ заголовок лекції.
- Це емоційна зачіпка — АЛЕ НЕ ЗАВЖДИ ПИТАННЯ. Обирай формат щоразу
  ІНШИЙ залежно від того, що краще пасує темі, з таких категорій:
  - твердження-шок: "ГЕН ВІЙНИ", "МОЗОК ВАС ОБМАНЮЄ"
  - цифра/факт: "90% ЛЮДЕЙ НЕ ЗНАЮТЬ", "3 СЕКУНДИ ДО ПАНІКИ"
  - пряме звернення/наказ: "ПЕРЕСТАНЬ СЕБЕ ОБМАНЮВАТИ", "НЕ ІГНОРУЙ ЦЕЙ ЗНАК"
  - незавершена думка/обрив: "КОЛИ СТРАХ КЕРУЄ ТОБОЮ...", "ЦЕ НЕ ПРОСТО ЗБІГ"
  - питання (лише ІНОДІ, не за замовчуванням): "ЩО ПІСЛЯ ГІПНОТЕРАПІЇ?"
  Питання — один із п'яти рівноправних варіантів, а не дефолтний формат:
  якщо не маєш підстав для іншого вибору, обирай НЕ питання.
- Приклади поганих: "РОЗПОВІДАЮ ПРО ГІПНОЗ", "ТЕМА СЬОГОДНІ: СТРАХ".

Правила dalle_prompt — стиль ЗАВЖДИ теплий, світлий, шамансько-балійський
медитативний (сонячне світло, природа, ритуальні предмети) — НІКОЛИ темний,
похмурий, зловісний чи тривожний, навіть якщо тема відео важка: контент сам
по собі світлий і заспокійливий, обкладинка має притягувати, а не
відштовхувати. РІЗНОМАНІТНІСТЬ у межах цього теплого стилю — обери
об'єкт/сцену, що підходить САМЕ ЦІЙ темі, а не завжди одне й те саме.
Обирай з різних категорій залежно від змісту, наприклад:
- природа й світло: сонячне проміння крізь листя/бамбук, ранковий туман у
  горах, схід сонця над океаном, роса на листі лотоса
- ритуальні/медитативні предмети: пахощі, свічки, чаша для співу, кристали
  й каміння, квіткові підношення (canang sari), чотки
- вода й спокій: хвилі на заході сонця, краплі води, тихе озеро
- людська постать ЗІ СПИНИ або в профілі, у спокійній/медитативній позі,
  підсвічена теплим світлом (ніколи обличчям до камери)
Уникай повторювати той самий тип об'єкта, що й у попередній обкладинці (якщо
з контексту видно попередню тему) — шукай несподіваний, але доречний і
теплий образ.
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
        y = 90 + int(COVER_HEIGHT * 0.10)   # відступ від верху (+10% висоти за проханням)

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


# ── "Подкаст-стиль": кадр із відео + інформативний текст ───────────────────────

WHITE = (255, 255, 255)

# Езотеричні палітри: (колір підзаголовка/початок градієнта, кінець градієнта,
# "біле" з відтінком, колір крапки в тегу). Одна випадкова на обкладинку.
PALETTES = [
    {"name": "amethyst",  "a": (190, 150, 255), "b": (255, 150, 215), "white": (247, 240, 255)},
    {"name": "aurora",    "a": (120, 240, 215), "b": (150, 175, 255), "white": (238, 255, 252)},
    {"name": "solar",     "a": (255, 214, 130), "b": (255, 150, 125), "white": (255, 248, 236)},
    {"name": "moonlight", "a": (235, 228, 255), "b": (165, 190, 255), "white": (250, 250, 255)},
    {"name": "emerald",   "a": (160, 240, 190), "b": (240, 220, 140), "white": (244, 255, 246)},
]
INK = (14, 8, 36)          # глибокий індиго замість чистого чорного (градієнти/тіні)

# ── Твої фото як фон (assets/photos) ─────────────────────────────────────────
# mode "cover" — кадрування під 9:16 із фокусом (fx, fy) — частки 0..1 точки,
# яку лишаємо в центрі; mode "fit" — фото ціле (розмиті поля), напр. арт-мандала.
PHOTOS_DIR = os.path.join(ASSETS_DIR, "photos")
PHOTO_META = {
    "flowers1.jpg":        {"mode": "cover", "fx": 0.38, "fy": 0.40},
    "flowers2.jpg":        {"mode": "cover", "fx": 0.42, "fy": 0.40},
    "mandala.jpg":         {"mode": "fit",   "y": 250},
    "ring.jpg":            {"mode": "cover", "fx": 0.65, "fy": 0.50},
    "gloves_sea.jpg":      {"mode": "cover", "fx": 0.45, "fy": 0.40},
    "yellow_notebook.jpg": {"mode": "cover", "fx": 0.50, "fy": 0.40},
    "yellow_portrait.jpg": {"mode": "cover", "fx": 0.45, "fy": 0.40},
    "piano.jpg":           {"mode": "cover", "fx": 0.45, "fy": 0.40},
}


def _crop_to_916(img: Image.Image, fx: float, fy: float) -> Image.Image:
    """Кадрує до 9:16 так, щоб точка фокусу (fx, fy) лишилась якомога ближче до центру."""
    W, H = COVER_WIDTH, COVER_HEIGHT
    ratio = W / H
    if img.width / img.height > ratio:        # ширше за 9:16 — ріжемо боки
        cw, ch = int(img.height * ratio), img.height
    else:                                      # вужче — ріжемо верх/низ
        cw, ch = img.width, int(img.width / ratio)
    x0 = min(max(int(img.width * fx - cw / 2), 0), img.width - cw)
    y0 = min(max(int(img.height * fy - ch / 2), 0), img.height - ch)
    return img.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.LANCZOS)


def _fit_with_blur(img: Image.Image, y: int) -> Image.Image:
    """Ціле фото по ширині кадру, а поля — розмита й затемнена копія того ж фото."""
    W, H = COVER_WIDTH, COVER_HEIGHT
    bg = _fit_cover(img, W, H).filter(ImageFilter.GaussianBlur(38))
    bg = Image.blend(bg, Image.new("RGB", (W, H), INK), 0.35)
    fg = img.resize((W, int(img.height * W / img.width)), Image.LANCZOS)
    bg.paste(fg, (0, y))
    return bg


def _random_photo_background() -> Image.Image:
    names = [n for n in PHOTO_META if os.path.exists(os.path.join(PHOTOS_DIR, n))]
    if not names:
        raise RuntimeError("У assets/photos немає фото")
    name = random.choice(names)
    meta = PHOTO_META[name]
    logger.info(f"Фон обкладинки: фото {name}")
    img = Image.open(os.path.join(PHOTOS_DIR, name)).convert("RGB")
    if meta["mode"] == "fit":
        return _fit_with_blur(img, meta.get("y", 250))
    return _crop_to_916(img, meta["fx"], meta["fy"])


_PLAN_TEXT_SYSTEM = """\
Ти робиш ТЕКСТ для обкладинки вертикального відео (психологія, гіпнотерапія,
стосунки, особистісний ріст) у стилі сильних подкаст-обкладинок: людина в
кадрі + плашка-тег + підзаголовок + великий заголовок. Обкладинка має
ЗМУШУВАТИ відкрити відео: конкретика, інтрига, обіцянка, особиста історія.

Поверни ТІЛЬКИ валідний JSON (без markdown):
{"tag": "...", "kicker": "...", "lines": ["...", "..."], "accent": [1]}

- tag: ОДНЕ слово ВЕЛИКИМИ — рубрика (напр. ПСИХОЛОГІЯ, ГІПНОТЕРАПІЯ,
  СТОСУНКИ, РОЗБІР, ІСТОРІЯ, ПОРАДА, ПІДКАСТ). Обери за змістом відео.
- kicker: 2-4 слова ВЕЛИКИМИ над заголовком — інтрига/контекст/цифра, що
  додає інформації, а не повторює заголовок. Напр. "УСЕ ВСЕРЕДИНІ",
  "ТРИ РІВНІ", "16 ГОДИН ПЕРЕЛЬОТУ", "ДОВГОСТРОКОВА СТРАТЕГІЯ".
- lines: заголовок, 2-3 рядки ВЕЛИКИМИ, разом до 9 слів, кожен рядок до ~16
  символів (рядки коротші за 18 символів!). Це головна зачіпка: що саме
  дізнається глядач / чого не знає / яка несподівана теза чи історія.
  Приклади стилю: ["НЕ ТРЕБА", "ШУКАТИ СЕБЕ"]; ["МОЄ «ХОЧУ»", "ЧИ «ТРЕБА»", "СУСПІЛЬСТВА?"];
  ["ВІРЮ. ЗНАЮ.", "ПРОЖИВАЮ"]; ["ЗАЛИШИВ ДІВЧИНУ", "І ПОЛЕТІВ", "НА БАЛІ"].
- accent: індекси рядків (з 0), які будуть ЗОЛОТИМИ — найсильніша частина
  заголовка (зазвичай останній рядок). Решта — білі.
- Мова — та сама, що в транскрипції. Ніяких фактів, яких немає в
  транскрипції; ніякого брехливого клікбейту; без крапки в кінці.
"""


def _plan_cover_text(transcript: str) -> dict:
    """GPT-4o → {"tag", "kicker", "lines": [...], "accent": [...]} (нормалізоване)."""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _PLAN_TEXT_SYSTEM},
            {"role": "user", "content": f"Транскрипція:\n\n{(transcript or '')[:2500]}"},
        ],
        temperature=0.8,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    lines = [str(l).strip().upper().rstrip(".") for l in (data.get("lines") or []) if str(l).strip()][:3]
    if not lines:
        raise ValueError("GPT не повернув заголовок")
    accent = [i for i in (data.get("accent") or []) if isinstance(i, int) and 0 <= i < len(lines)]
    if not accent:
        accent = [len(lines) - 1]
    return {
        "tag": str(data.get("tag") or "").strip().upper()[:16],
        "kicker": str(data.get("kicker") or "").strip().upper()[:34],
        "lines": lines,
        "accent": accent,
    }


def _text_w(draw, text, font, spacing=0):
    return int(draw.textlength(text, font=font)) + spacing * max(0, len(text) - 1)


def _draw_spaced(draw, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def _draw_gradient_text(layer: Image.Image, xy, text, font, c1, c2):
    """Текст із горизонтальним градієнтом c1 → c2 (маска тексту × градієнт)."""
    d = ImageDraw.Draw(layer)
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0] + 8, bbox[3] + 8
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=255)
    t = Image.linear_gradient("L").transpose(Image.ROTATE_90).resize((w, h))   # чорний→білий зліва направо
    grad = Image.composite(Image.new("RGB", (w, h), c2), Image.new("RGB", (w, h), c1), t)
    layer.paste(grad, (int(xy[0]), int(xy[1])), mask)


def _compose_cover(bg_image: Image.Image, plan: dict) -> str:
    """
    "Езотеричний подкаст-стиль": будь-який фон 1080×1920 (кадр відео / твоє
    фото / FLUX) + глибокий індиго-градієнт знизу, плашка-тег зліва зверху,
    підзаголовок із рискою та великий заголовок (відтінене біле + градієнт
    палітри). Палітра випадкова (PALETTES). Увесь текст — у "безпечній зоні"
    (y 240-1680): видно і в сітці профілю (обрізка 3:4), і під інтерфейсом.
    """
    W, H = COVER_WIDTH, COVER_HEIGHT
    pal = random.choice(PALETTES)
    bg = bg_image.resize((W, H), Image.LANCZOS).convert("RGBA")

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    g_top = 740
    for y in range(g_top, H):
        t = (y - g_top) / (H - g_top)
        gd.line([(0, y), (W, y)], fill=INK + (int(240 * min(1.0, t ** 0.8 * 1.15)),))
    for y in range(0, 340):
        gd.line([(0, y), (W, y)], fill=INK + (int(120 * (1 - y / 340)),))
    bg = Image.alpha_composite(bg, grad)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)

    # ── Заголовок: авто-розмір, щоб найдовший рядок вліз у ширину ────────────
    margin_x, max_w = 70, W - 140
    size = 124
    while size > 56:
        font = _load_font(size)
        if all(_text_w(d, ln, font) <= max_w for ln in plan["lines"]):
            break
        size -= 4
    font = _load_font(size)
    line_h = int(size * 1.13)
    bottom = 1560
    top = bottom - line_h * len(plan["lines"])

    for i, ln in enumerate(plan["lines"]):
        y = top + i * line_h
        sd.text((margin_x + 3, y + 5), ln, font=font, fill=(0, 0, 0, 210))
        if i in plan["accent"]:
            _draw_gradient_text(layer, (margin_x, y), ln, font, pal["a"], pal["b"])
        else:
            d.text((margin_x, y), ln, font=font, fill=pal["white"] + (255,))

    # ── Підзаголовок (колір палітри, з рискою під ним) ───────────────────────
    if plan.get("kicker"):
        kfont = _load_font(40)
        ky = top - 36 - 56
        sd.text((margin_x + 2, ky + 3), plan["kicker"], font=kfont, fill=(0, 0, 0, 190))
        _draw_spaced(d, (margin_x, ky), plan["kicker"], kfont, pal["a"] + (255,), 3)
        d.rectangle([margin_x, ky + 58, margin_x + 78, ky + 64], fill=pal["b"] + (255,))

    # ── Плашка-тег зліва зверху (світла, крапка кольору палітри) ─────────────
    if plan.get("tag"):
        tfont = _load_font(34)
        tw = _text_w(d, plan["tag"], tfont, 2)
        x0, y0, pad_x, h = 48, 262, 24, 62
        w = pad_x + 22 + 14 + tw + pad_x
        d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h // 2, fill=pal["white"] + (242,))
        d.ellipse([x0 + pad_x, y0 + h // 2 - 11, x0 + pad_x + 22, y0 + h // 2 + 11], fill=pal["b"] + (255,))
        _draw_spaced(d, (x0 + pad_x + 36, y0 + 11), plan["tag"], tfont, INK + (255,), 2)

    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    bg = Image.alpha_composite(bg, shadow)
    bg = Image.alpha_composite(bg, layer)

    if os.path.exists(LOGO_PATH):
        try:
            logo = _resize_logo(Image.open(LOGO_PATH).convert("RGBA"), 170)
            bg.paste(logo, (margin_x, 1600), mask=logo)
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
