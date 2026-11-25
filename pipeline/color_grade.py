"""
Мінімальний випадковий колорит відео: тепліше / холодніше / без змін.

Один вибір на вихідне відео (TikTok і Instagram-версії мають однаковий
колорит). Фільтр ffmpeg `colorbalance` + легка насиченість накладається лише
на сам кадр — до субтитрів, карток і вставок (їхні кольори не змінюються).
"""

import random

from config import COLOR_GRADE_ENABLED

# (назва, вага, ffmpeg-фільтр). Сила ~±4-5% по каналах — ледь помітно.
_GRADES = [
    ("warm", 40,
     "colorbalance=rs=0.03:bs=-0.03:rm=0.05:bm=-0.05:rh=0.03:bh=-0.04,eq=saturation=1.03"),
    ("cool", 40,
     "colorbalance=rs=-0.03:bs=0.04:rm=-0.04:bm=0.05:rh=-0.03:bh=0.04"),
    ("none", 20, None),
]


def pick_color_grade():
    """Повертає (назва, ffmpeg-фільтр або None)."""
    if not COLOR_GRADE_ENABLED:
        return "none", None
    name, _w, vf = random.choices(_GRADES, weights=[g[1] for g in _GRADES])[0]
    return name, vf
