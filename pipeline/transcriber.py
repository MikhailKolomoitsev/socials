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
        srt_content = _word_tuples_to_ass(word_tuples)
    else:
        # Fallback на segment-level, якщо API раптом не повернув слова.
        srt_content = _segments_to_ass(getattr(response, "segments", None) or [])

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
    return _word_tuples_to_ass(word_tuples)


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _segments_to_ass(segments: list) -> str:
    """Fallback: сегменти → ASS (коли word-level недоступний)."""
    chunks = []
    for seg in segments:
        start = _field(seg, "start", 0) or 0
        end = _field(seg, "end", 0) or 0
        text = (_field(seg, "text", "") or "").strip()
        if text:
            chunks.append((_render_chunk_text(text.split()), start, end))
    return _chunks_to_ass(chunks)


def _field(obj, key: str, default=None):
    """Дістає поле з об'єкта незалежно від того, dict це чи pydantic/звичайний об'єкт."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _seconds_to_ass_time(seconds: float) -> str:
    """Конвертує секунди у формат ASS: H:MM:SS.cc"""
    cs = int((seconds % 1) * 100)   # сотих секунди (ASS використовує .cc, не .ms)
    s = int(seconds) % 60
    m = int(seconds) // 60 % 60
    h = int(seconds) // 3600
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _word_tuples_to_ass(word_tuples: list, chunk_size: int = 3) -> str:
    """
    Групує слова по chunk_size і повертає ASS-вміст.
    ASS (замість SRT) дає повний контроль над шрифтом, розміром і позицією —
    subtitles filter в ffmpeg застосовує force_style непередбачувано для SRT.
    """
    words = [w for w in word_tuples if w[0]]
    if not words:
        return ""
    chunks_raw = [words[i:i + chunk_size] for i in range(0, len(words), chunk_size)]
    chunks = [
        (_render_chunk_text([w[0] for w in ch]), ch[0][1], ch[-1][2])
        for ch in chunks_raw
    ]
    return _chunks_to_ass(chunks)


# Параметри стилю субтитрів (TikTok-стиль)
_ASS_FONT_NAME  = "Montserrat ExtraBold"
_ASS_FONT_SIZE  = 32   # px у PlayRes-координатах (1080×1920) — було 30
_ASS_MARGIN_V   = 550  # px від нижнього краю (Alignment=2 → відступ знизу) — було 650, опущено на 100
_ASS_OUTLINE    = 3
_ASS_SHADOW     = 2
_ASS_HIGHLIGHT_COLOR = "&H00D7FF&"   # золотисто-жовтий (ASS BGR) для ключових слів
_ASS_DEFAULT_COLOR   = "&H00FFFFFF&"  # білий (з альфа-байтом 00 = непрозорий), повертаємось до нього після виділеного слова

# Короткі українські службові слова, які не виділяємо кольором (не несуть змістового навантаження)
_UK_STOPWORDS = {
    "і", "й", "та", "а", "але", "чи", "то", "б", "би", "ж", "же",
    "не", "ні", "це", "цей", "ця", "ці", "той", "те", "ти", "ви",
    "я", "ми", "він", "вона", "воно", "вони", "як", "що", "щоб", "коли",
    "де", "куди", "чому", "тому", "у", "в", "на", "з", "із", "зі", "до",
    "від", "по", "за", "під", "над", "про", "для", "без", "при", "після",
    "перед", "між", "через", "усе", "все", "весь", "вся", "тут", "там",
    "так", "дуже", "ще", "вже", "тільки", "лише", "теж", "також", "ось",
    "от", "мене", "тебе", "його", "її", "нас", "вас", "їх", "мій", "твій",
    "наш", "ваш", "свій", "буде", "був", "була", "були", "бути", "є",
}


def _strip_punct(word: str) -> str:
    return word.strip(".,!?…:;\"'()«»„“”-")


def _pick_highlight_word(words: list) -> str | None:
    """
    Обирає слово чанка для кольорового виділення — найдовше змістове
    слово (не службове, не коротке). Якщо в чанку немає такого — нічого
    не виділяємо (щоб не підсвічувати кожен прийменник).
    """
    candidates = [
        w for w in words
        if len(_strip_punct(w)) >= 4 and _strip_punct(w).lower() not in _UK_STOPWORDS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda w: len(_strip_punct(w)))


def _render_chunk_text(words: list) -> str:
    """
    Формує ASS-текст чанка: ключове слово обгортається кольоровим тегом
    {\\c...}, решта лишається білою (успадковує PrimaryColour зі стилю).
    """
    if not words:
        return ""
    highlight = _pick_highlight_word(words)
    if highlight is None:
        return " ".join(words)

    parts = []
    used = False
    for w in words:
        if not used and w == highlight:
            parts.append(f"{{\\c{_ASS_HIGHLIGHT_COLOR}}}{w}{{\\c{_ASS_DEFAULT_COLOR}}}")
            used = True
        else:
            parts.append(w)
    return " ".join(parts)


def _chunks_to_ass(chunks: list) -> str:
    """
    chunks: список (text, start_sec, end_sec)
    Повертає повний ASS-файл з заголовком і подіями.
    PlayResX/Y задаємо 1080×1920 — відповідає нормалізованому відео.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{_ASS_FONT_NAME},{_ASS_FONT_SIZE},"
        f"&H00FFFFFF,&H00000000,&H80000000,"  # білий текст, чорна обводка, напівпрозорий shadow
        f"-1,0,0,0,100,100,0,0,1,{_ASS_OUTLINE},{_ASS_SHADOW},"
        f"2,20,20,{_ASS_MARGIN_V},1\n"       # Alignment=2 (знизу по центру)
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for text, start, end in chunks:
        s = _seconds_to_ass_time(start)
        e = _seconds_to_ass_time(end)
        events.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


def _save_srt(content: str) -> str:
    """Зберігає ASS-файл (назва збережена для сумісності з рештою коду)."""
    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    return path
