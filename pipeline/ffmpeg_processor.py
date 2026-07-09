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


def to_standard_mp4(input_path: str) -> str:
    """
    Конвертує будь-який формат (MOV, HEVC/H.265, змінний FPS тощо) у
    стандартний H.264 + AAC mp4 з фіксованим 30fps.

    Використовує ultrafast preset — якість трохи нижча ніж fast, але в 3-4x
    швидше. Це проміжний файл для обробки, тому якість некритична —
    фінальна якість визначається кроком burn_subtitles (де crf=18).
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_std.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-r", "30",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg помилка (to_standard_mp4): {tail}")
    return output_path


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
    """
    Надійний trim+concat через concat demuxer:
      1. Кожен сегмент кодується окремо (→ temp файл з keyframe на початку)
      2. Всі temp файли об'єднуються через concat demuxer з -c copy

    Попередній підхід (один filter_complex на всі сегменти) давав frame=0
    після to_standard_mp4 бо trim filter не міг знайти keyframe у потрібний
    момент при постійному FPS H.264 потоці.
    """
    temp_files = []
    concat_list_lines = []

    try:
        for i, (start, end) in enumerate(segments):
            seg_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_seg{i}.mp4")
            temp_files.append(seg_path)

            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start:.3f}",
                "-to", f"{end:.3f}",
                "-i", input_path,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-avoid_negative_ts", "make_zero",
                seg_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                tail = "\n".join(result.stderr.strip().splitlines()[-10:])
                raise RuntimeError(f"ffmpeg помилка (trim сегмент {i}): {tail}")

            concat_list_lines.append(f"file '{seg_path}'")

        # Записуємо список сегментів у тимчасовий файл
        concat_txt = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_concat.txt")
        temp_files.append(concat_txt)
        with open(concat_txt, "w") as f:
            f.write("\n".join(concat_list_lines))

        # Склеюємо без перекодування
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c", "copy",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stderr.strip().splitlines()[-10:])
            raise RuntimeError(f"ffmpeg помилка (concat): {tail}")

    finally:
        for p in temp_files:
            try:
                os.remove(p)
            except Exception:
                pass


def normalize_vertical(input_path: str, width: int = 1080, height: int = 1920) -> str:
    """
    Приводить будь-яке відео (горизонтальне, квадратне, "майже вертикальне")
    до строгого 9:16 (1080×1920) для TikTok.

    На відміну від простого crop (який обрізає й може зрізати важливу
    частину кадру, напр. голову), тут кадр вписується ПОВНІСТЮ — порожні
    смуги зверху/збоку заповнюються розмитим збільшеним фоном з того ж
    відео (як у TikTok/Reels/Stories), а не чорними полями.

    Якщо відео вже рівно 1080×1920 — нічого не робимо й повертаємо
    оригінальний шлях (без зайвого перекодування).
    """
    cw, ch = _probe_resolution(input_path)
    if cw == width and ch == height:
        return input_path

    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_vertical.mp4")

    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:5[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
    )

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-vcodec", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-acodec", "aac", "-b:a", "128k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg помилка (normalize_vertical): {tail}")

    return output_path


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
        font_size = _clamp(round(height / 55), 18, 42)
    margin_v = _clamp(round(height * 0.22), 140, 380)  # відступ від низу, щоб не лізти під UI TikTok
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


def extract_audio(video_path: str) -> str:
    """
    Витягує аудіодоріжку з відео у форматі mp3.

    Whisper API має ліміт 25MB на файл. Відео після обробки може важити
    50-200MB, але аудіо з того ж відео — лише 3-8MB (mp3 128kbps).
    Передаємо в Whisper тільки аудіо, а не весь відеофайл.

    Returns:
        Шлях до .mp3 файлу
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_audio.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",                   # без відеопотоку
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-ar", "16000",          # 16kHz — оптимально для Whisper
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        raise RuntimeError(f"ffmpeg помилка (extract_audio): {tail}")
    return output_path


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
