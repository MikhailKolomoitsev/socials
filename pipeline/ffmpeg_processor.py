"""
FFmpeg обробка відео:
  1. Видалення тиші (silence removal)
  2. Burn-in субтитрів з .srt файлу
"""

import os
import uuid
import ffmpeg
from config import TMP_DIR


def remove_silence(input_path: str, silence_threshold: float = -35.0, min_silence_duration: float = 0.5) -> str:
    """
    Видаляє паузи з відео.

    Args:
        input_path: шлях до вхідного відео
        silence_threshold: поріг тиші в dB (за замовчуванням -35 dB)
        min_silence_duration: мінімальна тривалість тиші для видалення (секунди)

    Returns:
        Шлях до обробленого відео
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nosilence.mp4")

    (
        ffmpeg
        .input(input_path)
        .audio.filter(
            "silenceremove",
            stop_periods=-1,
            stop_duration=min_silence_duration,
            stop_threshold=f"{silence_threshold}dB",
        )
        .output(output_path, vcodec="copy", acodec="aac")
        .overwrite_output()
        .run(quiet=True)
    )

    return output_path


def burn_subtitles(input_path: str, srt_path: str, font_size: int = 22, font_color: str = "white") -> str:
    """
    Накладає субтитри на відео.

    Args:
        input_path: шлях до відео (після silence removal)
        srt_path: шлях до .srt файлу
        font_size: розмір шрифту субтитрів
        font_color: колір тексту

    Returns:
        Шлях до фінального відео з субтитрами
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_final.mp4")

    # Екрануємо шлях для ffmpeg subtitles filter
    escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:")

    subtitle_style = (
        f"FontSize={font_size},"
        f"PrimaryColour=&H00{_color_to_bgr(font_color)},"
        "Bold=1,"
        "Alignment=2,"          # знизу по центру
        "MarginV=30"
    )

    (
        ffmpeg
        .input(input_path)
        .output(
            output_path,
            vf=f"subtitles={escaped_srt}:force_style='{subtitle_style}'",
            vcodec="libx264",
            acodec="copy",
            crf=18,
        )
        .overwrite_output()
        .run(quiet=True)
    )

    return output_path


def extract_frame(video_path: str, timestamp: float = 1.0) -> str:
    """Витягує кадр з відео для обкладинки."""
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_frame.jpg")

    (
        ffmpeg
        .input(video_path, ss=timestamp)
        .output(output_path, vframes=1, format="image2", vcodec="mjpeg")
        .overwrite_output()
        .run(quiet=True)
    )

    return output_path


def _color_to_bgr(color_name: str) -> str:
    colors = {
        "white": "FFFFFF",
        "yellow": "00FFFF",
        "black": "000000",
    }
    return colors.get(color_name.lower(), "FFFFFF")
