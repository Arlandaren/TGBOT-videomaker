#!/usr/bin/env python3
"""Превью стилей обложки для Mini App: по одной картинке 270×480 на стиль.

Запускается руками, когда меняется стиль или добавляется новый, результат
коммитится:

    python tools/cover_previews.py

Пишет `src/webapp/static/covers/<key>.jpg`. Фон нарисованный
(`cover.sample_background`), без фотографий людей: превью показывает стиль
надписи, а не чьё-то лицо. Обложка собирается в полном размере 1080×1920 и
только потом уменьшается — так на превью те же пропорции и переносы строк, что
получит пользователь, а не отдельная вёрстка под маленький кадр.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image  # noqa: E402

from src.pipeline import cover  # noqa: E402

OUT_DIR = REPO_ROOT / "src" / "webapp" / "static" / "covers"
SAMPLE_TEXT = "ТОП 3 ОШИБКИ В МОНТАЖЕ"
SIZE = (270, 480)
MAX_BYTES = 30_000


def render(key: str, background) -> bytes:  # noqa: ANN001 — BGR-массив numpy
    full = cover.compose(background, SAMPLE_TEXT, key)
    small = full.resize(SIZE, Image.Resampling.LANCZOS)
    # Качество 80, а если картинка не уложилась в лимит — ниже, шагами.
    data = b""
    for quality in (80, 74, 68, 62, 56):
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        data = buf.getvalue()
        if len(data) <= MAX_BYTES:
            break
    return data


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    background = cover.sample_background(1080, 1920)
    for key in cover.style_keys():
        data = render(key, background)
        path = OUT_DIR / f"{key}.jpg"
        path.write_bytes(data)
        print(f"{path.relative_to(REPO_ROOT).as_posix()}  {len(data) / 1024:.1f} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
