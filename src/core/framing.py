"""Крупность по кускам: EDL → интервалы на выходном таймлайне.

Чистая арифметика без ffmpeg и без OpenCV. Жила она в `pipeline/crop.py`, но
нужна двум потребителям сразу: кроп по этим интервалам меняет окно, а камера
рендера (`core/camera.py`) по ним же ведёт наезд. Границы обязаны совпасть
кадр в кадр — поэтому формула одна, и лежит она в `core`, а не дублируется.
"""

from __future__ import annotations

from typing import Sequence

from src.core.edl import EDL

Span = tuple[float, float, str]


def _merge_spans(spans: Sequence[Span]) -> list[Span]:
    out: list[Span] = []
    for start, end, framing in spans:
        if end - start <= 1e-6:
            continue
        if out and out[-1][2] == framing and abs(out[-1][1] - start) < 1e-6:
            out[-1] = (out[-1][0], end, framing)
        else:
            out.append((start, end, framing))
    return out


def framing_spans(edl: EDL, *, applied_cuts: bool = True) -> list[Span]:
    """EDL → интервалы крупности в том времени, которое увидит `autocrop`.

    Кроп работает уже после нарезки, поэтому времена нужны выходные: kept-куски
    склеены встык, дырок между ними нет.

    `applied_cuts=False` значит, что паузы не вырезаны и настоящих резов в
    картинке нет вообще. Тогда любая смена крупности пришлась бы на середину
    непрерывного плана и читалась бы как самопроизвольный зум — весь ролик
    снимаем одной крупностью.
    """
    kept = sorted(edl.kept_cuts, key=lambda c: c.src_in)
    if not kept:
        return []
    if not applied_cuts:
        return [(0.0, edl.source_duration, "wide")]
    spans: list[Span] = []
    acc = 0.0
    for c in kept:
        spans.append((acc, acc + c.duration, c.framing))
        acc += c.duration
    return _merge_spans(spans)


# Насколько покрытие может разойтись с фактической длительностью, чтобы разницу
# можно было считать накопленным округлением кадров, а не другим таймлайном.
SPAN_DRIFT_TOLERANCE = 0.05


def _fit_spans(spans: Sequence[Span], duration: float) -> list[Span]:
    """Границы крупности — на кадровую сетку файла, который увидит кроп.

    `framing_spans` складывает ТОЧНЫЕ длительности EDL, а `cut.py` режет каждый
    кусок фильтром `fps=` и склеивает, то есть каждый кусок округляется до целого
    числа кадров. Ошибка копится по числу резов: на эталонном EDL три kept-куска
    (4.565 + 6.225 + 7.68 = 18.47 с) дают в cut.mp4 445 кадров при 24 fps, то есть
    18.54 с — плюс 1.7 кадра. Раньше эту разницу целиком съедал последний span, а
    все внутренние границы оставались ранними, и смена крупности приходилась не на
    рез, а на несколько кадров ВНУТРЬ предыдущего плана — то есть выглядела
    самопроизвольным зумом, ради запрета которого вся схема и построена.

    Растягиваем покрытие пропорционально: округление набегает примерно поровну на
    кусок, поэтому пропорция раскладывает его обратно почти без остатка. Правка
    применяется только когда расхождение похоже на округление кадров
    (SPAN_DRIFT_TOLERANCE); всё, что больше, — это другой таймлайн, и его честнее
    оставить клипу.
    """
    covered = spans[-1][1] - spans[0][0]
    if covered <= 0 or spans[0][0] > 1e-6:
        return list(spans)
    k = duration / covered
    if abs(k - 1.0) > SPAN_DRIFT_TOLERANCE:
        return list(spans)
    return [(s * k, e * k, f) for s, e, f in spans]


def normalize_spans(spans: Sequence[Span] | None, duration: float) -> list[Span]:
    """Интервалы крупности → непрерывное покрытие [0, duration] без дырок."""
    if duration <= 0:
        return [(0.0, 0.0, "wide")]
    if spans:
        spans = _fit_spans(sorted(spans, key=lambda sp: sp[0]), duration)
    clipped = [
        (max(0.0, s), min(duration, e), f if f in ("wide", "tight") else "wide")
        for s, e, f in sorted(spans or [], key=lambda sp: sp[0])
        if min(duration, e) - max(0.0, s) > 1e-6
    ]
    if not clipped:
        return [(0.0, duration, "wide")]

    out: list[Span] = []
    cursor = 0.0
    for start, end, framing in clipped:
        if start > cursor + 1e-6:
            # Дырку отдаём предыдущей крупности: смена обязана происходить на резе,
            # а в середине куска для неё нет повода.
            out.append((cursor, start, out[-1][2] if out else framing))
        out.append((max(cursor, start), end, framing))
        cursor = end
    if duration - cursor > 1e-6:
        out.append((cursor, duration, out[-1][2]))
    return _merge_spans(out)
