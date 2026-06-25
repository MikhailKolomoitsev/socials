"""
FFmpeg обробка відео:
  1. Видалення тиші (silence removal)
  2. Burn-in субтитрів з .srt файлу
"""

import json
import os
import re
import subprocess
import uuid
import ffmpeg
from config import TMP_DIR


def _run(stream):
    """
    Запускає ffmpeg-команду і, якщо вона впаде, піднімає помилку з реальним
    текстом stderr (а не загальним "ffmpeg error (see stderr output for detail)",
    яке ховає справжню причину при run(quiet=True)).
    """
    try:
        stream.run(quiet=True, capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="ignore")
        # Останні рядки stderr зазвичай містять саму причину помилки
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg помилка: {tail or 'немає stderr'}") from e


def remove_silence(input_path: str, silence_threshold: float = -35.0, min_silence_duration: float = 0.5) -> str:
    """
    Видаляє паузи з відео (синхронно з відео- і аудіопотоку).

    Важливо: попередня версія застосовувала фільтр "silenceremove" тільки до
    .audio і повертала лише його — відеопотік повністю зникав з вихідного
    файлу (через .audio.filter(...).output(...) у ffmpeg-python мапиться
    тільки той один потік, на якому викликано .output()). Через це наступні
    кроки пайплайну (burn_subtitles, extract_frame) отримували відео без
    жодного відеокадру і extract_frame падав з "ffmpeg error".

    Тепер: детектуємо тихі інтервали через ffmpeg silencedetect, інвертуємо
    їх у список "живих" шматків і виконуємо trim+concat одночасно для
    відео й аудіо, щоб вони лишались синхронними.

    Args:
        input_path: шлях до вхідного відео
        silence_threshold: поріг тиші в dB (за замовчуванням -35 dB)
        min_silence_duration: мінімальна тривалість тиші для видалення (секунди)

    Returns:
        Шлях до обробленого відео
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_nosilence.mp4")

    duration = _probe_duration(input_path)
    silence_intervals = _detect_silence(input_path, silence_threshold, min_silence_duration)
    keep_segments = _invert_intervals(silence_intervals, duration)

    if not keep_segments or len(keep_segments) == 1 and keep_segments[0] == (0.0, duration):
        # Тиші не знайдено — просто копіюємо файл без перекодування.
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            tail = "\n".join(result.stderr.strip().splitlines()[-15:])
            raise RuntimeError(f"ffmpeg помилка (copy): {tail}")
        return output_path

    _trim_and_concat(input_path, keep_segments, output_path)
    return output_path


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe помилка: {result.stderr.strip()}")
    return float(json.loads(result.stdout)["format"]["duration"])


def _detect_silence(path: str, threshold_db: float, min_duration: float) -> list:
    """Повертає список (start, end) тихих інтервалів через ffmpeg silencedetect."""
    cmd = [
        "ffmpeg", "-i", path,
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", stderr)]

    return list(zip(starts, ends))


def _invert_intervals(silence: list, duration: float) -> list:
    """Перетворює список тихих інтервалів на список 'живих' (keep) інтервалів."""
    keep = []
    cursor = 0.0
    for start, end in silence:
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    # Прибираємо надто короткі шматки (артефакти детекції)
    return [(s, e) for s, e in keep if e - s > 0.05]


def _trim_and_concat(input_path: str, segments: list, output_path: str):
    filter_parts = []
    concat_inputs = []
    for i, (start, end) in enumerate(segments):
        filter_parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")

    filter_complex = ";".join(filter_parts) + ";" + "".join(concat_inputs) + \
        f"concat=n={len(segments)}:v=1:a=1[outv][outa]"

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-vcodec", "libx264", "-acodec", "aac",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg помилка (trim+concat): {tail}")


def burn_subtitles(input_path: str, srt_path: str, font_size: int = None, font_color: str = "white") -> str:
    """
    Накладає субтитри на відео у "TikTok-стилі": жирний білий текст з
    товстою чорною обводкою й тінню (читається на будь-якому фоні),
    розмір шрифту масштабується відносно роздільної здатності відео
    (фіксовані 22px були практично невидимі на 1080×1920).

    Args:
        input_path: шлях до відео (після silence removal)
        srt_path: шлях до .srt файлу
        font_size: розмір шрифту субтитрів; якщо None — рахується від
                   висоти відео (~ height/24, з розумними межами)
        font_color: колір тексту

    Returns:
        Шлях до фінального відео з субтитрами
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_final.mp4")

    # Явна перевірка перед запуском ffmpeg: якщо .srt раптом відсутній або
    # порожній (диск забитий, файл ще не "доїхав" до диска тощо) — піднімаємо
    # зрозумілу помилку замість незрозумілого libass "Unable to open ...".
    if not os.path.exists(srt_path):
        raise RuntimeError(f"SRT файл не знайдено перед burn-in субтитрів: {srt_path}")
    if os.path.getsize(srt_path) == 0:
        raise RuntimeError(f"SRT файл порожній перед burn-in субтитрів: {srt_path}")

    width, height = _probe_resolution(input_path)

    if font_size is None:
        font_size = _clamp(round(height / 24), 32, 110)
    margin_v = _clamp(round(height * 0.12), 60, 260)  # відступ від низу, щоб не лізти під UI TikTok
    outline = max(2, round(font_size / 14))
    shadow = max(1, round(font_size / 28))

    # Екрануємо шлях для ffmpeg subtitles filter
    escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:")

    subtitle_style = (
        "FontName=DejaVu Sans,"
        f"FontSize={font_size},"
        f"PrimaryColour=&H00{_color_to_bgr(font_color)},"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        f"Outline={outline},"
        f"Shadow={shadow},"
        "Bold=1,"
        "Alignment=2,"          # знизу по центру
        f"MarginV={margin_v}"
    )

    _run(
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
    )

    return output_path


def _probe_resolution(path: str) -> tuple:
    """Повертає (width, height) відео через ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", path,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe помилка (resolution): {result.stderr.strip()}")
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def extract_frame(video_path: str, timestamp: float = 1.0) -> str:
    """Витягує кадр з відео для обкладинки."""
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_frame.jpg")

    duration = _probe_duration(video_path)
    if timestamp >= duration:
        # Якщо відео коротше за обраний таймстемп — беремо середину відео.
        timestamp = max(0.0, duration / 2)

    _run(
        ffmpeg
        .input(video_path, ss=timestamp)
        .output(output_path, vframes=1, format="image2", vcodec="mjpeg")
        .overwrite_output()
    )

    return output_path


def _color_to_bgr(color_name: str) -> str:
    colors = {
        "white": "FFFFFF",
        "yellow": "00FFFF",
        "black": "000000",
    }
    return colors.get(color_name.lower(), "FFFFFF")
