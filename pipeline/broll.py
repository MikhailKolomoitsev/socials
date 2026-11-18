"""
Анімовані вставки-образи на ключові ідеї в перших секундах відео.

Наприклад, спікер каже "божественний партнер" — на 1.8 с на весь екран
з'являється AI-картинка: силует пари, довкола космічна енергія. Таких вставок
1-2 на відео, лише на початку (BROLL_WINDOW).

Як працює:
  1. GPT читає перші ~30 с транскрипту з таймкодами, обирає 1-2 образні фрази
     й пише для кожної англомовний промпт сцени (_plan_inserts).
  2. fal.ai FLUX (cover_generator._fal_generate) генерує кадр 9:16 у єдиному
     "космічному" стилі (BROLL_STYLE); кадри генеруються паралельно.
  3. ffmpeg робить із кадру короткий кліп: плавне наближення (Ken Burns),
     легке світіння (bloom), віньєтка і пульсація яскравості.
  4. ffmpeg_processor.burn_subtitles накладає кліп у момент фрази (з плавним
     появленням/зникненням) у тому ж проході, що й хуки та субтитри.

Будь-яка помилка НЕ валить пайплайн: build_broll повертає None (або лише ті
вставки, що вдались), і відео обробляється як раніше.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

from config import TMP_DIR, OPENAI_API_KEY
from pipeline.cover_generator import _fal_generate

logger = logging.getLogger(__name__)

BROLL_DURATION = 1.8          # секунд на одну вставку
BROLL_FPS = 30
BROLL_MAX = 2
WINDOW_START, WINDOW_END = 1.0, 25.0   # коли може починатись вставка
MIN_START_GAP = 4.0           # мінімум між початками двох вставок
END_MARGIN = 0.8              # вставка закінчується щонайменше за стільки до кінця відео

# Спільна основа стилю всіх вставок (впізнаваність): кінематографічний,
# мальований, глибокі кольори + світлові акценти. Сама сцена й палітра
# підбираються GPT під ідею (космос — для духовного, місто/природа/побут — для
# приземленого), тому тут немає жорсткого "космічного" кольору.
BROLL_STYLE = (
    "Cinematic ethereal painterly digital art, rich deep colors with luminous "
    "glowing light accents, floating light particles, soft volumetric light and "
    "rim lighting, dramatic high contrast, ultra detailed. Strictly NO text, "
    "NO letters, NO numbers, NO watermarks, NO logos, NO recognizable faces "
    "(silhouettes or faceless figures only). Vertical 9:16 composition, the main "
    "subject centered in the upper-middle of the frame, clear empty space in the "
    "lower third."
)

_PLAN_SYSTEM = """\
Ти арт-директор коротких вертикальних відео про психологію, гіпнотерапію,
стосунки та особистісний ріст. Вибери 1-2 моменти в ПЕРШИХ секундах відео, де
ключову ідею варто підсилити короткою візуальною вставкою (1-2 секунди), і
напиши промпт для генерації зображення.

Вхід: транскрипт з таймкодами (перші ~30 секунд).

Яку фразу брати:
- образну, емоційну, містичну чи метафоричну, яку легко показати картинкою:
  "божественний партнер", "внутрішня дитина", "страх як тінь", "розірвати
  ланцюг", "дорога до себе", "родове прокляття", "підсвідомість";
- НЕ бери привітання, службові й загальні фрази, абстракції без образу
  ("важливо", "тому що", "сьогодні поговоримо").
- Якщо жодна фраза не годиться — поверни порожній список (це нормально).

Поверни ТІЛЬКИ валідний JSON (без markdown):
{"inserts": [{"phrase": "...", "prompt": "..."}]}

phrase: 1-4 слова ТОЧНО як у транскрипті — на початку цієї фрази з'явиться картинка.

prompt (англійською, 35-60 слів) — ОДНА сильна сцена:
- один головний об'єкт-символ у центрі, чіткий силует; люди — лише силуети,
  без облич і без дрібних деталей рук;
- ідея виражена образно й підібрана ПІД ТЕМУ, а не завжди космос:
  духовне, стосунки, підсвідомість — космос, світлова енергія, туманності;
  гроші, кар'єра, звички — місто, дороги, двері, ключі, сходи, годинники,
  архітектура; тривога, емоції — вода, туман, буря, вогонь, грозове небо;
  тіло й здоров'я — природа, рослини, ліс, світанок, потік світла у тілі;
  вибирай ту сцену, що найточніше передає САМЕ цю ідею;
- опиши світло й кольори (глибокі насичені тони під настрій ідеї + один
  яскравий світловий акцент), атмосферу, глибину (передній/задній план),
  частинки світла;
- жодного тексту, цифр, літер, логотипів.

Приклади:
- "божественний партнер" → "Two human silhouettes standing very close, facing
  each other, their bodies outlined by flowing ribbons of luminous gold and
  violet cosmic energy that spiral around them, a vast galaxy nebula glowing
  behind them, tiny stars and light particles drifting upward, soft volumetric
  rim light, deep indigo space"
- "внутрішня дитина" → "A small child silhouette standing alone in a vast
  misty field under a sky full of stars, holding a glowing orb of warm golden
  light that illuminates drifting particles, violet twilight, serene and
  magical atmosphere"
- "страх" → "A towering dark shadow silhouette dissolving into drifting violet
  smoke while a single beam of golden light cuts through the darkness from
  above, glowing dust particles, dramatic contrast, mystical atmosphere"
- "фінансова свобода" → "A lone silhouette standing before a huge glowing open
  door at the end of a long dark corridor of marble steps, warm golden light
  pouring out and casting a long path of light, floating golden dust, deep
  teal and amber tones, cinematic depth"
- "розірвати замкнене коло звичок" → "A giant circular chain of rusted links
  breaking apart in mid-air, the broken links glowing orange-hot and
  scattering sparks, a small silhouette standing in the center looking up,
  stormy teal sky, dramatic backlight, floating embers"
- "тривога" → "A calm silhouette standing waist-deep in dark stormy water under
  a swirling night sky, while a soft ring of pale blue light spreads around
  them and calms the waves, drifting mist, glowing particles, deep navy and
  silver tones"
"""


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def _timed_head(word_tuples: list) -> str:
    """Перші ~30 с транскрипту рядками по ~3 с: [0:07] текст…"""
    lines, bucket, bucket_start = [], [], None
    for word, start, _end in word_tuples:
        if start > 30:
            break
        if bucket_start is None:
            bucket_start = start
        bucket.append(word)
        if start - bucket_start >= 3.0:
            lines.append((bucket_start, " ".join(bucket)))
            bucket, bucket_start = [], None
    if bucket:
        lines.append((bucket_start, " ".join(bucket)))
    return "\n".join(f"[{int(t) // 60}:{int(t) % 60:02d}] {text}" for t, text in lines)


def _locate(phrase: str, word_tuples: list):
    """Початок фрази (секунди) за словами транскрипту; None, якщо не знайдено."""
    target = [_norm(w) for w in phrase.split() if _norm(w)]
    if not target:
        return None
    words = [(_norm(w), s) for w, s, _e in word_tuples]
    for i in range(len(words)):
        if words[i][0] == target[0] and [w for w, _ in words[i:i + len(target)]] == target:
            return words[i][1]
    for w, s in words:                      # запасний варіант: перше слово фрази
        if w == target[0]:
            return s
    return None


def _plan_inserts(word_tuples: list, duration: float) -> list:
    """Повертає до BROLL_MAX dict {"start", "prompt", "phrase"} без накладань."""
    if not OPENAI_API_KEY or not word_tuples:
        return []
    head = _timed_head(word_tuples)
    if not head:
        return []

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": f"Транскрипція з таймкодами:\n\n{head}"},
        ],
        temperature=0.7,
        max_tokens=500,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)

    inserts = []
    for item in data.get("inserts") or []:
        phrase = str(item.get("phrase") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        start = _locate(phrase, word_tuples) if phrase else None
        if start is None or not prompt:
            continue
        inserts.append({"start": start, "prompt": prompt, "phrase": phrase})

    inserts.sort(key=lambda x: x["start"])
    chosen = []
    for ins in inserts:
        if not (WINDOW_START <= ins["start"] <= WINDOW_END):
            continue
        if ins["start"] + BROLL_DURATION > duration - END_MARGIN:
            continue
        if chosen and ins["start"] - chosen[-1]["start"] < MIN_START_GAP:
            continue
        chosen.append(ins)
    return chosen[:BROLL_MAX]


def _render_clip(image_path: str, out_path: str) -> None:
    """Кадр → кліп: наближення + bloom + віньєтка + пульсація яскравості."""
    n = int(BROLL_DURATION * BROLL_FPS)
    vf = (
        # 2× апскейл — щоб zoompan не "тремтів" на цілих пікселях
        "scale=2160:3840:flags=lanczos,"
        f"zoompan=z='1+0.14*sin(PI/2*on/{n})':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)-40*on/{n}':d={n}:s=1080x1920:fps={BROLL_FPS},"
        "split[a][b];"
        "[b]gblur=sigma=40,eq=saturation=1.3[bl];"
        "[a][bl]blend=all_mode=screen:all_opacity=0.35,"
        "vignette=PI/4,"
        "eq=brightness='0.04*sin(6*t)':eval=frame,"
        "format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", image_path,
        "-vf", vf, "-t", f"{BROLL_DURATION:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-an", "-movflags", "+faststart", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        raise RuntimeError(f"ffmpeg помилка (broll clip): {tail}")


def _make_one(ins: dict, out_dir: str, index: int) -> dict:
    img = _fal_generate(ins["prompt"], style_override=BROLL_STYLE)
    image_path = os.path.join(out_dir, f"b{index}.jpg")
    img.save(image_path, "JPEG", quality=93)
    clip_path = os.path.join(out_dir, f"b{index}.mp4")
    _render_clip(image_path, clip_path)
    return {"path": clip_path, "start": ins["start"], "duration": BROLL_DURATION, "dir": out_dir}


def build_broll(word_tuples: list, duration: float):
    """
    Повертає список dict для ffmpeg_processor.burn_subtitles(broll=...):
      [{"path": ".../b0.mp4", "start": 5.2, "duration": 1.8, "dir": ...}, ...]
    або None. Після використання викликач викликає cleanup_broll.
    """
    out_dir = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_broll")
    try:
        plan = _plan_inserts(word_tuples, duration)
        if not plan:
            return None
        os.makedirs(out_dir, exist_ok=True)
        for p in plan:
            logger.info(f"B-roll @ {p['start']:.1f}s «{p['phrase']}»: {p['prompt'][:90]}…")

        results = []
        with ThreadPoolExecutor(max_workers=len(plan)) as pool:
            futures = [pool.submit(_make_one, p, out_dir, i) for i, p in enumerate(plan)]
            for f in futures:
                try:
                    results.append(f.result())
                except Exception as e:        # одна невдала вставка не блокує іншу
                    logger.warning(f"B-roll вставку пропущено ({e})")
        if not results:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None
        return results
    except Exception as e:
        logger.warning(f"B-roll пропущено ({e})", exc_info=True)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None


def cleanup_broll(broll) -> None:
    for d in {b["dir"] for b in broll or []}:
        shutil.rmtree(d, ignore_errors=True)
