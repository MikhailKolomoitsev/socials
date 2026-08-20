"""
Транскрипція аудіо → .ass субтитри, у кількох стилях (по одному на платформу).
Підтримує OpenAI Whisper API та AssemblyAI (вибирається через config).

Навіщо кілька стилів: TikTok і Instagram (і, ймовірно, YouTube Shorts у
майбутньому) детектять контент, що вже опубліковано десь-інде
("crossposting"/дублікат), і штучно ріжуть охоплення такому відео. Тому для
кожної платформи рендеримо ОКРЕМЕ фінальне відео — той самий кліп, але з
трохи іншим кольором/розміром/позицією субтитрів, тобто помітно різними
пікселями саме там, де платформа найімовірніше рахує perceptual hash.

Транскрипцію (виклик Whisper/AssemblyAI) робимо ОДИН раз (transcribe_words),
а сам ASS для кожного стилю рендеримо окремо (build_ass_for_style) — без
повторних викликів платного API.
"""

import os
import random
import uuid
from config import TMP_DIR, OPENAI_API_KEY, ASSEMBLYAI_API_KEY
from pipeline.ffmpeg_processor import extract_audio, has_audio_stream


# ── Стилі субтитрів під платформу ────────────────────────────────────────────
#
# Щоб додати ще одну платформу (напр. "youtube_shorts") — достатньо додати
# новий ключ сюди з власним набором параметрів. main.py і queue_runner.py
# більше нічого міняти не треба: main.py рендерить/вивантажує по одному
# відео на кожен ключ SUBTITLE_STYLES, і зберігає URL у db.videos у
# колонку s3_url_<platform> (крім "tiktok" — той лишається в s3_url для
# зворотної сумісності зі старими рядками БД).
SUBTITLE_STYLES = {
    "tiktok": {
        "font_name": "Montserrat ExtraBold",
        "font_size": 52,
        "margin_v": 500,
        "outline": 3,
        "shadow": 2,
        "highlight_color": "&H00D7FF&",   # золотисто-жовтий (ASS BGR)
        "default_color": "&H00FFFFFF&",   # білий, alpha=00 (непрозорий)
    },
    "instagram": {
        "font_name": "Montserrat ExtraBold",
        "font_size": 54,
        "margin_v": 560,                  # трохи вище за TikTok
        "outline": 3,
        "shadow": 2,
        "highlight_color": "&H6C30E1&",   # рожево-фіолетовий (Instagram-акцент), ASS BGR
        "default_color": "&H00FFFFFF&",
    },
}


def transcribe_words(video_path: str) -> tuple[list, list, str]:
    """
    Транскрибує відео і повертає (word_tuples, segments, plain_text) — БЕЗ
    побудови ASS, щоб той самий результат транскрипції можна було
    відрендерити в кілька стилів через build_ass_for_style(), не викликаючи
    Whisper/AssemblyAI повторно (платний API).

    word_tuples: список (word, start, end); порожній список, якщо API не
                 повернув word-level таймстемпи.
    segments: сирі сегменти (fallback для build_ass_for_style, якщо
              word_tuples порожній).
    """
    if not has_audio_stream(video_path):
        # Відео без аудіодоріжки (напр. зняте без звуку) — extract_audio
        # інакше впав би з "Output file does not contain any stream".
        # Порожня транскрипція — той самий шлях, що й "Whisper нічого не
        # розпізнав": main.py пропускає субтитри й підпис, обробка триває.
        return [], [], ""
    if OPENAI_API_KEY:
        return _transcribe_whisper_words(video_path)
    elif ASSEMBLYAI_API_KEY:
        return _transcribe_assemblyai_words(video_path)
    else:
        raise ValueError("Не задано OPENAI_API_KEY або ASSEMBLYAI_API_KEY у .env")


def build_ass_for_style(word_tuples: list, segments: list, style_name: str) -> str:
    """Рендерить ASS-субтитри з уже готової транскрипції у стилі style_name
    (див. SUBTITLE_STYLES). Не звертається до жодного зовнішнього API."""
    style = SUBTITLE_STYLES[style_name]
    words = [w for w in word_tuples if w[0]]
    if words:
        return _word_tuples_to_ass(words, style)
    return _segments_to_ass(segments, style)


def transcribe_to_srt(video_path: str, style_name: str = "tiktok") -> tuple[str, str]:
    """Зворотна сумісність: один виклик = транскрипція + ASS ОДНИМ стилем.
    Використовуй transcribe_words() + build_ass_for_style() там, де потрібно
    кілька стилів з однієї транскрипції (так робить main.py)."""
    word_tuples, segments, plain_text = transcribe_words(video_path)
    ass_content = build_ass_for_style(word_tuples, segments, style_name)
    srt_path = save_ass(ass_content)
    return srt_path, plain_text


# ── OpenAI Whisper ─────────────────────────────────────────────────────────────

def _transcribe_whisper_words(video_path: str) -> tuple[list, list, str]:
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
    segments = getattr(response, "segments", None) or []

    # ВАЖЛИВО: модель Transcription у openai==1.35.0 описує тільки поле
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
    return word_tuples, segments, plain_text


# ── AssemblyAI ─────────────────────────────────────────────────────────────────

def _transcribe_assemblyai_words(video_path: str) -> tuple[list, list, str]:
    import assemblyai as aai

    aai.settings.api_key = ASSEMBLYAI_API_KEY
    transcriber = aai.Transcriber()

    transcript = transcriber.transcribe(video_path)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")

    words = transcript.words or []
    word_tuples = [(w.text, w.start / 1000, w.end / 1000) for w in words]
    return word_tuples, [], (transcript.text or "")


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _segments_to_ass(segments: list, style: dict) -> str:
    """Fallback: сегменти → ASS (коли word-level недоступний)."""
    chunks = []
    for seg in segments:
        start = _field(seg, "start", 0) or 0
        end = _field(seg, "end", 0) or 0
        text = (_field(seg, "text", "") or "").strip()
        if text:
            chunks.append((_render_chunk_text(text.split(), style), start, end))
    return _chunks_to_ass(chunks, style)


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


def _word_tuples_to_ass(words: list, style: dict) -> str:
    """
    Групує слова в чанки ЗМІННОГО розміру (1-3 слова) — динамічний ритм.
    ASS (замість SRT) дає повний контроль над шрифтом, розміром і позицією —
    subtitles filter в ffmpeg застосовує force_style непередбачувано для SRT.
    """
    if not words:
        return ""
    chunks_raw = _dynamic_chunks(words)
    chunks = [
        (_render_chunk_text([w[0] for w in ch], style), ch[0][1], ch[-1][2])
        for ch in chunks_raw
    ]
    return _chunks_to_ass(chunks, style)


def _dynamic_chunks(words: list, solo_probability: float = 0.4) -> list:
    """
    Розбиває список (word, start, end) на чанки змінного розміру 1-3 слова —
    замість фіксованих чанків по 3, які виглядають монотонно.

    Логіка: змістове слово (довге, не службове — те саме правило, що й для
    кольорового виділення в _pick_highlight_word) з імовірністю
    solo_probability показується САМЕ, одним словом — "панч"-ефект. Інакше
    слова групуються по 2-3 разом, як типовий TikTok-стиль субтитрів.
    """
    chunks = []
    i, n = 0, len(words)
    while i < n:
        word_text = _strip_punct(words[i][0])
        is_content_word = len(word_text) >= 5 and word_text.lower() not in _UK_STOPWORDS

        if is_content_word and random.random() < solo_probability:
            size = 1
        else:
            size = random.choice([2, 3])

        size = min(size, n - i)
        chunks.append(words[i:i + size])
        i += size
    return chunks


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


def _render_chunk_text(words: list, style: dict) -> str:
    """
    Формує ASS-текст чанка: ключове слово обгортається кольоровим тегом
    {\\c...}, решта лишається білою (успадковує PrimaryColour зі стилю).
    Колір виділення береться зі style — різний для кожної платформи.
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
            parts.append(f"{{\\c{style['highlight_color']}}}{w}{{\\c{style['default_color']}}}")
            used = True
        else:
            parts.append(w)
    return " ".join(parts)


def _chunks_to_ass(chunks: list, style: dict) -> str:
    """
    chunks: список (text, start_sec, end_sec)
    Повертає повний ASS-файл з заголовком і подіями.
    PlayResX/Y задаємо 1080×1920 — відповідає нормалізованому відео.
    Fontsize/MarginV беруться зі style — різні для кожної платформи.
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
        f"Style: Default,{style['font_name']},{style['font_size']},"
        f"&H00FFFFFF,&H00000000,&H80000000,"  # білий текст, чорна обводка, напівпрозорий shadow
        f"-1,0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},"
        f"2,20,20,{style['margin_v']},1\n"       # Alignment=2 (знизу по центру)
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


def save_ass(content: str) -> str:
    """Зберігає ASS-файл у TMP_DIR і повертає шлях до нього."""
    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    return path
