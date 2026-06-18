"""
Транскрипція аудіо → .srt субтитри.
Підтримує OpenAI Whisper API та AssemblyAI (вибирається через config).
"""

import os
import uuid
from config import TMP_DIR, OPENAI_API_KEY, ASSEMBLYAI_API_KEY


def transcribe_to_srt(video_path: str) -> tuple[str, str]:
    """
    Транскрибує відео і повертає (srt_path, plain_text).

    Returns:
        srt_path: шлях до .srt файлу
        transcript: plain text транскрипція
    """
    if OPENAI_API_KEY:
        return _transcribe_whisper(video_path)
    elif ASSEMBLYAI_API_KEY:
        return _transcribe_assemblyai(video_path)
    else:
        raise ValueError("Не задано OPENAI_API_KEY або ASSEMBLYAI_API_KEY у .env")


# ── OpenAI Whisper ─────────────────────────────────────────────────────────────

def _transcribe_whisper(video_path: str) -> tuple[str, str]:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)

    with open(video_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )

    segments = response.segments or []
    srt_content = _segments_to_srt(segments)
    plain_text = response.text or ""

    srt_path = _save_srt(srt_content)
    return srt_path, plain_text


# ── AssemblyAI ─────────────────────────────────────────────────────────────────

def _transcribe_assemblyai(video_path: str) -> tuple[str, str]:
    import assemblyai as aai

    aai.settings.api_key = ASSEMBLYAI_API_KEY
    transcriber = aai.Transcriber()

    transcript = transcriber.transcribe(video_path)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")

    # Конвертуємо utterances в SRT
    srt_content = _assemblyai_to_srt(transcript)
    plain_text = transcript.text or ""

    srt_path = _save_srt(srt_content)
    return srt_path, plain_text


def _assemblyai_to_srt(transcript) -> str:
    lines = []
    words = transcript.words or []

    # Групуємо по ~5 слів на рядок субтитра
    chunk_size = 5
    chunks = [words[i:i + chunk_size] for i in range(0, len(words), chunk_size)]

    for i, chunk in enumerate(chunks, start=1):
        start_ms = chunk[0].start
        end_ms = chunk[-1].end
        text = " ".join(w.text for w in chunk)
        lines.append(f"{i}\n{_ms_to_srt_time(start_ms)} --> {_ms_to_srt_time(end_ms)}\n{text}\n")

    return "\n".join(lines)


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _segments_to_srt(segments: list) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        start = _seconds_to_srt_time(seg.get("start", 0))
        end = _seconds_to_srt_time(seg.get("end", 0))
        text = seg.get("text", "").strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _seconds_to_srt_time(seconds: float) -> str:
    ms = int((seconds % 1) * 1000)
    s = int(seconds) % 60
    m = int(seconds) // 60 % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ms_to_srt_time(ms: int) -> str:
    return _seconds_to_srt_time(ms / 1000)


def _save_srt(content: str) -> str:
    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.srt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
