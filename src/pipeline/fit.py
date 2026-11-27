"""Готовый ролик должен пролезть в чат.

Облачный Bot API принимает от бота файл до 50 МБ. Рендер же держал постоянное
качество (crf): ~5 Мбит/с видео и 320 кбит/с звука, то есть ролик 52 с весил
33 МБ, а всё длиннее ~75 с в чат не уходило — при разрешённых трёх минутах.

Здесь два слоя.

1. `render_cap` — потолок битрейта для рендера, посчитанный по длине ролика.
   Качество по-прежнему задаёт crf, потолок только не даёт файлу перерасти
   предел. Короткие ролики до него не доходят и выходят как раньше; длинный
   ролик теряет в битрейте ровно столько, сколько нужно. Это бесплатно: второго
   прохода кодека нет. Замер 2026-11-27: монтаж 2:28 с потолком 2359 кбит/с
   вышел 46.2 МБ; без потолка, по битрейту прежних роликов, было бы около 98.
2. `for_chat` — подстраховка при отправке. Файл, который всё равно больше
   предела (ролики, собранные до этой правки, бэкенд `ass`), пережимается в
   копию под предел. Оригинал в хранилище не меняется.

Со своим сервером Bot API (`BOT_API_URL`) предел — 2 ГБ, и ничего из этого не
срабатывает.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.proc import affmpeg

log = logging.getLogger(__name__)

# Пределы Bot API на отправку файла ботом, МБ.
CLOUD_SEND_MAX_MB = 50
LOCAL_SEND_MAX_MB = 2000
# Мегабайт считаем десятичным: какой из двух имеет в виду Telegram, в описании
# API не сказано, а ошибиться в меньшую сторону ничего не стоит.
MB = 1_000_000

# Целимся ниже предела. Потолок битрейта (VBV) держит средний поток, но не
# каждый отрезок: файл вправе перерасти расчёт на объём буфера (две секунды
# потока), плюс служебные таблицы mp4.
MARGIN = 0.92

# Звук. Remotion без указаний кодирует AAC в 320 кбит/с — на трёх минутах это
# 7 МБ из 46. Пока видео хватает места, звук не трогаем; когда места мало —
# 128 кбит/с, для речи с музыкой разница не слышна.
AUDIO_DEFAULT_KBPS = 320
AUDIO_TIGHT_KBPS = 128
# «Видео хватает места» — это столько. Поток при crf 22 на живой съёмке
# 1080×1920 — около 5000 кбит/с (замер по готовым роликам на сервере).
VIDEO_ROOMY_KBPS = 4000
# Выше этого потолок не мешает никакому ролику — не задаём его вовсе, чтобы
# кодек работал ровно как раньше.
NO_CAP_KBPS = 20_000
# Ниже этого картинку 1080×1920 уже не спасти; до такого доходит только при
# поднятом `MAX_VIDEO_MINUTES` (три минуты — это ~1900).
VIDEO_FLOOR_KBPS = 300


class TooBigToSend(RuntimeError):
    """Файл больше предела чата, и ужать его не вышло."""


@dataclass(frozen=True, slots=True)
class Budget:
    video_kbps: int
    # None — звук кодируется как обычно (`AUDIO_DEFAULT_KBPS`).
    audio_kbps: int | None

    @property
    def total_kbps(self) -> int:
        return self.video_kbps + (self.audio_kbps or AUDIO_DEFAULT_KBPS)


def send_limit_bytes(cfg: Any) -> int:
    """Сколько байт бот может отправить одним файлом."""
    mb = cfg.result_max_mb or (LOCAL_SEND_MAX_MB if cfg.bot_api_url else CLOUD_SEND_MAX_MB)
    return int(mb * MB)


def budget(duration: float, limit_bytes: int) -> Budget:
    """Битрейты, с которыми ролик такой длины уложится в `limit_bytes`."""
    total = limit_bytes * MARGIN * 8 / max(duration, 1.0) / 1000
    if total - AUDIO_DEFAULT_KBPS >= VIDEO_ROOMY_KBPS:
        return Budget(int(total - AUDIO_DEFAULT_KBPS), None)
    return Budget(max(VIDEO_FLOOR_KBPS, int(total - AUDIO_TIGHT_KBPS)), AUDIO_TIGHT_KBPS)


def render_cap(duration: float, cfg: Any) -> Budget | None:
    """Потолок для рендера или None, если ролику такой длины он не нужен."""
    b = budget(duration, send_limit_bytes(cfg))
    return b if b.video_kbps < NO_CAP_KBPS else None


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


async def for_chat(path: Path, limit_bytes: int) -> Path:
    """Файл, который можно отправить: сам `path` или ужатая копия рядом.

    Копию (`<имя>.fit.mp4`) убирает вызывающий. Не получилось ужать —
    `TooBigToSend`: отправлять нет смысла, Telegram файл не примет.
    """
    size = _size(path)
    if size is None or size <= limit_bytes:
        return path

    from src.pipeline.probe import probe

    fitted = path.with_suffix(".fit.mp4")
    try:
        info = await asyncio.to_thread(probe, path)
        b = budget(info.duration, limit_bytes)
        log.warning(
            "ролик %s весит %.1f МБ при пределе %.0f — пережимаю копию до %d кбит/с",
            path.name, size / MB, limit_bytes / MB, b.video_kbps,
        )
        await affmpeg(
            # По два потока на чтение и на кодек. Сжатие идёт в процессе БОТА, а
            # у него на сервере 768 МБ на всё. Замер 2026-11-27, файл 129 МБ,
            # 2:36, 1080×1920: четыре потока — 63 с и пик 500 МБ, два — 83 с и
            # 320 МБ. Двадцать секунд не стоят риска, что ядро снимет бота.
            ["-threads", "2", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
             # Один проход со средним битрейтом и тем же потолком: быстрее двух
             # проходов, а точности хватает — запас заложен в `MARGIN` (тот же
             # замер: цель 46.0 МБ, вышло 46.1).
             "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", f"{b.video_kbps}k", "-maxrate", f"{b.video_kbps}k",
             "-bufsize", f"{b.video_kbps * 2}k", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", f"{b.audio_kbps or AUDIO_DEFAULT_KBPS}k",
             "-threads", "2", "-movflags", "+faststart", str(fitted)],
            timeout=1800,
        )
    except Exception as e:  # noqa: BLE001 — причина любая, итог один: не отправить
        fitted.unlink(missing_ok=True)
        raise TooBigToSend(f"{path.name}: {size / MB:.1f} МБ, ужать не вышло: {e}") from e

    got = _size(fitted) or 0
    if not 0 < got <= limit_bytes:
        fitted.unlink(missing_ok=True)
        raise TooBigToSend(
            f"{path.name}: после сжатия {got / MB:.1f} МБ при пределе {limit_bytes / MB:.0f}"
        )
    return fitted
