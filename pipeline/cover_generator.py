"""
Генерація кастомної обкладинки для TikTok / Instagram Reels.
Бере кадр з відео і накладає текст + логотип у твоєму стилі.

Поклади свій логотип як assets/logo.png (прозорий PNG).
"""

import os
import uuid
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config import TMP_DIR

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")

# Розміри для Reels/TikTok (9:16)
COVER_WIDTH = 1080
COVER_HEIGHT = 1920


def generate_cover(frame_path: str, title_text: str = "", subtitle_text: str = "") -> str:
    """
    Генерує обкладинку 1080×1920.

    Args:
        frame_path: кадр з відео (jpg/png)
        title_text: великий текст зверху (можна залишити порожнім)
        subtitle_text: менший текст знизу

    Returns:
        Шлях до готової обкладинки
    """
    # 1. Відкриваємо кадр і масштабуємо до cover size
    bg = Image.open(frame_path).convert("RGB")
    bg = _fit_cover(bg, COVER_WIDTH, COVER_HEIGHT)

    # 2. Легке затемнення для читабельності тексту
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 90))
    bg = bg.convert("RGBA")
    bg = Image.alpha_composite(bg, overlay)

    draw = ImageDraw.Draw(bg)

    # 3. Логотип (якщо є)
    if os.path.exists(LOGO_PATH):
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo = _resize_logo(logo, max_width=220)
        logo_x = (COVER_WIDTH - logo.width) // 2
        bg.paste(logo, (logo_x, 80), mask=logo)

    # 4. Заголовок
    if title_text:
        font_title = _load_font(size=72)
        _draw_text_centered(draw, title_text, font_title, y=COVER_HEIGHT // 2 - 80, color=(255, 255, 255))

    # 5. Підзаголовок
    if subtitle_text:
        font_sub = _load_font(size=42)
        _draw_text_centered(draw, subtitle_text, font_sub, y=COVER_HEIGHT // 2 + 20, color=(220, 220, 220))

    # 6. Зберігаємо
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_cover.jpg")
    bg.convert("RGB").save(output_path, "JPEG", quality=95)
    return output_path


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Масштабує і обрізає зображення до точного розміру (cover fit)."""
    img_ratio = img.width / img.height
    target_ratio = w / h

    if img_ratio > target_ratio:
        new_h = h
        new_w = int(img.width * h / img.height)
    else:
        new_w = w
        new_h = int(img.height * w / img.width)

    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def _resize_logo(logo: Image.Image, max_width: int) -> Image.Image:
    if logo.width > max_width:
        ratio = max_width / logo.width
        return logo.resize((max_width, int(logo.height * ratio)), Image.LANCZOS)
    return logo


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_text_centered(draw: ImageDraw.Draw, text: str, font, y: int, color: tuple):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (COVER_WIDTH - text_w) // 2
    # Тінь
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 180))
    draw.text((x, y), text, font=font, fill=color)
