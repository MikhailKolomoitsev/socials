"""
Транскрипція аудіо → .srt субтитри.
Підтримує OpenAI Whisper API та AssemblyAI (вибирається через config).
"""

import os
import uuid
from config import TMP_DIR, OPENAI_API_KEY, ASSEMBLYAI_API_KEY
from pipeline.ffmpeg_processor import extract_audio


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
    import logging
    logger = logging.getLogger(__name__)
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Витягуємо лише аудіо (mp3 16kHz ~5MB) замість повного відео (50-200MB).
    # Whisper API має ліміт 25MB — відео легко його перевищує, аудіо — ніколи.
    audio_path = extract_audio(video_path)
    try:
        # Спочатку пробуємо з явною мовою uk (щоб не плутало з російською)
        with open(audio_path, "rb") as f:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="uk",
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )

        # Якщо Whisper повернув порожній результат з language=uk —
        # повторюємо без мовного фільтру (auto-detect). Краще будь-які
        # субтитри, ніж взагалі без них.
        if not (response.text or "").strip():
            logger.warning("Whisper з language=uk повернув порожній текст — повторюю без мовного фільтру")
            with open(audio_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                )
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass

    plain_text = response.text or ""
    words = getattr(response, "words", None) or []

    if words:
        # Word-level → короткі "панчові" субтитри (3-4 слова на кадр),
        # синхронні з мовленням — TikTok-стиль, а не цілі речення одразу.
        #
        # ВАЖЛИВО: модель Transcription у openai==1.35.0 описує тільше поле
        # "text" — words/segments приходять як "extra"-поля (model_config
        # extra="allow") і потрапляють сюди як звичайні dict, А НЕ як
        # pydantic-об'єкти. getattr(dict, "word", default) завжди повертає
        # default, бо в dict немає атрибутів — тільки ключі. Тому читаємо
        # через _field(), який працює і з dict, і з об'єктом.
        word_tuples = [
            (
                (_field(w, "word", "") or "").strip(),
                _field(w, "start", 0) or 0,
                _field(w, "end", 0) or 0,
            )
            for w in words
        ]
        srt_content = _word_tuples_to_srt(word_tuples)
    else:
        # Fallback на segment-level, якщо API раптом не повернув слова.
        srt_content = _segments_to_srt(getattr(response, "segments", None) or [])

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
    words = transcript.words or []
    word_tuples = [(w.text, w.start / 1000, w.end / 1000) for w in words]
    return _word_tuples_to_srt(word_tuples)


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _segments_to_srt(segments: list) -> str:
    """
    segments у різних SDK/відповідях бувають і dict, і pydantic-об'єктами
    (залежно від версії openai та того, чи поле типізоване чи "extra") —
    тому читаємо через _field(), яка підтримує обидва варіанти.
    """
    lines = []
    for i, seg in enumerate(segments, start=1):
        start = _seconds_to_srt_time(_field(seg, "start", 0) or 0)
        end = _seconds_to_srt_time(_field(seg, "end", 0) or 0)
        text = (_field(seg, "text", "") or "").strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _field(obj, key: str, default=None):
    """Дістає поле з об'єкта незалежно від того, dict це чи pydantic/звичайний об'єкт."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _seconds_to_srt_time(seconds: float) -> str:
    ms = int((seconds % 1) * 1000)
    s = int(seconds) % 60
    m = int(seconds) // 60 % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _word_tuples_to_srt(word_tuples: list, chunk_size: int = 3) -> str:
    """
    word_tuples: список (text, start_seconds, end_seconds) у хронологічному порядку.

    Групує слова по chunk_size на один кадр субтитра — короткі "панчові"
    фрази, що з'являються синхронно з мовленням (TikTok-стиль), а не
    цілі речення одночасно на екрані.
    """
    words = [w for w in word_tuples if w[0]]
    if not words:
        return ""

    lines = []
    chunks = [words[i:i + chunk_size] for i in range(0, len(words), chunk_size)]
    for i, chunk in enumerate(chunks, start=1):
        start = _seconds_to_srt_time(chunk[0][1])
        end = _seconds_to_srt_time(chunk[-1][2])
        text = " ".join(w[0] for w in chunk)
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _save_srt(content: str) -> str:
    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.srt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())  # гарантуємо, що дані реально на диску до return,
        # а не тільки в буфері процесу — щоб наступний крок (burn_subtitles)
        # точно знайшов файл, навіть якщо диск під навантаженням.
    return path
