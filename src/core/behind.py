"""Заголовок за головой: решение по маске человека. Чистая логика, без numpy.

Титр первых секунд может уйти ЗА голову говорящего: текст рисуется поверх
фона, а человек его перекрывает. Порядок слоёв в рендере при этом не меняется —
на блок титра накладывается покадровая маска «не человек», и текст исчезает
там, где стоит голова.

Приём хорош в узком коридоре. Голова обязана закрыть заметную, но небольшую
часть большого слова: меньше — приёма не видно, больше — слово не читается.
Поэтому решение принимается ПО САМОЙ МАСКЕ, на каждом кадре жизни титра
(`verdict`), а место титра по вертикали подбирается так, чтобы в коридор
попасть (`pick_shift`). Любой промах — и титр рисуется как раньше: приём не
может сделать ролик хуже.

Маску считает `src/pipeline/matte.py`; здесь только то, что проверяется без
видео и без модели.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Sequence

from pydantic import BaseModel, ConfigDict

from src.core.geometry import (
    Rect,
    line_box_height,
    line_gap,
    line_rise,
    measure_line,
    metrics_for,
    SizedWord,
)

# Коридор: какую долю большого слова закрывает человек. Медиана по кадрам — в
# этих границах; на отдельном кадре допустимо чуть шире (`BIG_MIN`, `BIG_MAX`).
BIG_MEDIAN = (0.08, 0.35)
BIG_MIN = 0.04
BIG_MAX = 0.45
# К чему стремится подбор места: голова заходит на нижнюю треть слова.
BIG_TARGET = 0.18
# Верх букв обязан остаться: по нему слово и читается.
TOP_MAX = 0.12
# Ни одна буква не пропадает целиком.
LETTER_MAX = 0.6
# Подпись над большим словом человек не закрывает вовсе.
SMALL_MAX = 0.03
# Доля «неуверенных» пикселей маски в полосе слова: модель не поняла, где край.
SOFT_MAX = 0.15
# Насколько доля закрытого гуляет за жизнь титра: человек двигается.
RANGE_MAX = 0.15


@dataclass(frozen=True, slots=True)
class Coverage:
    """Что человек закрывает на ОДНОМ кадре. Все числа — доли, 0..1."""

    big: float  # коробка большого слова
    big_top: float  # её верхние 45 %
    letter: float  # самая закрытая буква
    small: float  # коробка подписи над словом
    soft: float  # неуверенные пиксели маски в полосе слова


def verdict(frames: Sequence[Coverage]) -> str | None:
    """Причина отказа или `None`, если титр может уйти за голову."""
    if not frames:
        return "нет кадров для замера"
    bigs = [f.big for f in frames]
    median = statistics.median(bigs)
    if any(f.soft > SOFT_MAX for f in frames):
        return "маска неуверенная у края слова"
    if median < BIG_MEDIAN[0] or min(bigs) < BIG_MIN:
        return "голова не касается большого слова"
    if median > BIG_MEDIAN[1] or max(bigs) > BIG_MAX:
        return "голова закрывает слишком большую часть слова"
    if max(bigs) - min(bigs) > RANGE_MAX:
        return "человек двигается: закрытая часть слова гуляет"
    if any(f.big_top > TOP_MAX for f in frames):
        return "закрыт верх букв — слово не прочитать"
    if any(f.letter > LETTER_MAX for f in frames):
        return "одна из букв закрыта почти целиком"
    if any(f.small > SMALL_MAX for f in frames):
        return "человек закрывает подпись над словом"
    return None


def pick_shift(table: Mapping[float, Sequence[Coverage]]) -> float | None:
    """Сдвиг титра по вертикали (px), при котором приём выглядит лучше всего.

    Из сдвигов, прошедших `verdict`, берётся тот, где медианная доля закрытого
    ближе всего к `BIG_TARGET`; при равенстве — меньший по модулю, чтобы титр
    без нужды не уходил из своей полосы.
    """
    passed = [
        (abs(statistics.median(f.big for f in frames) - BIG_TARGET), abs(dy), dy)
        for dy, frames in table.items()
        if verdict(frames) is None
    ]
    return min(passed)[2] if passed else None


def span_frames(start: float, end: float, fps: int, preset: Any) -> tuple[int, int]:
    """Первый и последний кадр, на которых титр виден.

    Блок живёт дольше своего конца на хвост ухода — то же число, что
    `blockOutFrames` в Captions.tsx: рендерер держит маску на тех же кадрах.
    """
    a = preset.animation
    tail_ms = max(a.block_out_ms, getattr(a, "block_overlap_min_ms", 220))
    tail = max(8, _js_round(tail_ms / 1000 * fps))
    return _js_round(start * fps), _js_round(end * fps) + tail


def _js_round(value: float) -> int:
    """`Math.round`: половина — вверх. Питоновский `round` округляет к чётному,
    и на кадре ровно посередине маска разошлась бы с рендерером на кадр."""
    return math.floor(value + 0.5)


class TitleBoxes(NamedTuple):
    """Где на кадре лежат чернила титра (в покое, без сдвига)."""

    big: Rect  # большое слово: от верха прописной буквы до базовой линии
    small: Rect | None  # подпись над ним; `None` — титр из одного слова
    letters: int  # сколько букв в большом слове — на столько колонок оно делится


def title_boxes(block: Any, card_preset: Any, frame: tuple[int, int]) -> TitleBoxes:
    """Коробки чернил титра по его раскладке.

    Меряется НЕ строчный бокс, а сами буквы: у титра межстрочие 0.5, строчные
    боксы заходят друг на друга и про то, где нарисовано слово, не говорят
    ничего. Чернила — от верха прописной буквы до базовой линии; выносные вниз
    не в счёт, их голова закрывает первыми, и это не мешает чтению.
    """
    width, _ = frame
    metrics = metrics_for(card_preset)
    cap = (metrics.cap_height or metrics.ascender) / metrics.units_per_em
    panel = card_preset.panel
    top = block.rect.y + ((panel.padding_y + panel.border_width) if panel.enabled else 0.0)
    gap = line_gap(card_preset)

    inks: list[tuple[Rect, list[Any]]] = []
    for line in block.lines:
        sizes = [w.size or float(card_preset.font.size) for w in line]
        wide = measure_line(
            [SizedWord(w.text, s) for w, s in zip(line, sizes)],
            metrics=metrics,
            letter_spacing=card_preset.font.letter_spacing,
            base_size=float(card_preset.font.size),
            word_spacing=card_preset.font.word_spacing,
        )
        baseline = top + line_rise(sizes, card_preset, metrics=metrics)
        tall = cap * max(sizes)
        inks.append((Rect((width - wide) / 2, baseline - tall, wide, tall), line))
        top += line_box_height(sizes, card_preset, metrics=metrics) + gap

    big_at = max(range(len(inks)), key=lambda i: inks[i][0].height)
    big, big_line = inks[big_at]
    rest = [ink for i, (ink, _) in enumerate(inks) if i != big_at]
    small = None
    if rest:
        x0 = min(r.x for r in rest)
        y0 = min(r.y for r in rest)
        x1 = max(r.x + r.width for r in rest)
        y1 = max(r.y + r.height for r in rest)
        small = Rect(x0, y0, x1 - x0, y1 - y0)
    letters = max(1, sum(len(w.text) for w in big_line if w.text))
    return TitleBoxes(big=big, small=small, letters=letters)


class BehindDecision(BaseModel):
    """Итог замера: уходит ли титр за голову, и если да — как.

    Пишется рядом с масками (`matte.json`) и уезжает в `build_plan`. `dir` пуст
    — приём не сработал, `reason` говорит почему; титр рисуется как раньше.
    """

    model_config = ConfigDict(extra="forbid")

    # Папка с масками `NNNNN.png` (номер — кадр выходного ролика).
    dir: str = ""
    first: int = 0
    last: int = 0
    # Сдвиг титра по вертикали относительно места из темы, px кадра.
    dy: float = 0.0
    reason: str | None = None
    # Медианная доля большого слова, закрытая головой, и время замера — в лог.
    cover: float | None = None
    seconds: float | None = None

    @property
    def on(self) -> bool:
        return bool(self.dir) and self.reason is None and self.last >= self.first
