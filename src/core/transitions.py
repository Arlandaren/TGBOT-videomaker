"""Переходы: короткое движение видео на склейке, начале мысли и ударном слове.

Жёсткий рез в говорящей голове читается как дефект записи, а не как монтаж:
кадр дёргается на долю лица, и больше ничего не происходит. Переход превращает
этот рывок в приём — видео на треть секунды наезжает, въезжает сбоку, вспыхивает
или трясётся и встаёт на место.

Откуда берутся:

* лист графики (`EDL.visuals.transitions`) — их расставил режиссёр графики или
  бесплатный расстановщик (`pipeline/visuals.py`), только номерами слов;
* ударные слова субтитров (`emphasis == "shout"`) — наезд на само слово.

Здесь из них получаются секунды и кадры (`plan.meta["cuts"]`), и здесь же
держатся потолки: частота, начало и конец ролика, вход и выход сцены. Рендерер
(`render/src/visuals/motion.mjs`, `cutAt`) только считает формулу по номеру
кадра — решений там нет (§0), случайности тоже.

Двигается ТОЛЬКО видео: субтитры и графика стоят. Поэтому там, где субтитр
привязан к голове или уходит за неё, движущий переход заменяется вспышкой —
иначе текст на эти кадры разошёлся бы с лицом.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from src.core.edl import EDL, Timeline
from src.core.framing import SPAN_DRIFT_TOLERANCE
from src.core.overlay_models import (
    MIN_ANIM_FRAMES,
    SHOUT_PUNCH_GAP,
    SHOUT_PUNCH_MAX,
    TRANSITION_HEAD,
    TRANSITION_MIN_GAP,
    TRANSITION_TAIL,
)

log = logging.getLogger(__name__)

# Сколько длится переход, с. Треть секунды: короче восьми кадров движение
# читается как дефект, дольше — как самостоятельная сцена.
CUT_SECONDS = 0.34
# Переход на слове рядом со склейкой прилипает к ней: слово начинается чуть
# позже реза, а движение обязано стартовать ровно с новым куском.
CUT_SNAP = 0.3
# Вход и выход сцены уже двигают кадр — второе движение поверх читалось бы кашей.
SCENE_GUARD = 0.1
MOVING_KINDS = ("punch", "slide_left", "slide_right", "shake")


def cut_times(edl: EDL, *, apply_cuts: bool, duration: float) -> list[float]:
    """Секунды склеек на выходном таймлайне: границы оставленных кусков.

    Та же арифметика, что в `framing.framing_spans`, но соседние куски одной
    крупности не склеиваются — рез есть рез. Накопленное округление кадров
    раскладывается пропорцией, как в `framing._fit_spans`.
    """
    kept = sorted(edl.kept_cuts, key=lambda c: c.src_in)
    if not apply_cuts or len(kept) < 2:
        return []
    total = sum(c.duration for c in kept)
    k = duration / total if total > 0 else 1.0
    if abs(k - 1.0) > SPAN_DRIFT_TOLERANCE:
        k = 1.0
    out: list[float] = []
    acc = 0.0
    for cut in kept[:-1]:
        acc += cut.duration
        out.append(acc * k)
    return out


def _scene_guards(plan: Any) -> list[tuple[float, float]]:
    """Окна входа и выхода сцен: в них переходов нет."""
    fps = plan.fps
    out: list[tuple[float, float]] = []
    for scene in ((plan.meta or {}).get("visuals") or {}).get("scenes") or []:
        start, end = float(scene["start"]), float(scene["end"])
        out.append((start - SCENE_GUARD, start + scene["in_frames"] / fps + SCENE_GUARD))
        out.append((end - scene["out_frames"] / fps - SCENE_GUARD, end + SCENE_GUARD))
    return out


def _pinned(plan: Any, start: float, end: float) -> bool:
    """Стоит ли в это время субтитр, привязанный к голове или уходящий за неё."""
    return any(
        (block.anchor_path or block.behind is not None) and block.start < end and block.end > start
        for block in plan.blocks
    )


def _shouts(plan: Any) -> list[float]:
    """Начала ударных слов, прореженные до потолка ролика."""
    starts = sorted(
        word.start
        for block in plan.blocks
        for line in block.lines
        for word in line
        if word.emphasis == "shout"
    )
    out: list[float] = []
    for at in starts:
        if len(out) >= SHOUT_PUNCH_MAX:
            break
        if out and at - out[-1] < SHOUT_PUNCH_GAP:
            continue
        out.append(at)
    return out


def plan_cuts(edl: EDL, plan: Any, *, apply_cuts: bool = True) -> list[dict[str, Any]]:
    """Лист + готовый план → переходы рендера: `[{at, frames, kind}]`.

    Приоритет при тесноте: переход на настоящей склейке, потом ударное слово,
    потом переход на начале мысли. Склейка важнее: там кадр и так дёргается, и
    без перехода рывок остался бы дефектом.
    """
    fps = plan.fps
    duration = plan.duration
    if fps <= 0 or duration <= 0 or getattr(edl, "split_until", None) is not None:
        return []
    frames = max(MIN_ANIM_FRAMES, round(CUT_SECONDS * fps))
    length = frames / fps

    timeline = edl.timeline() if apply_cuts else Timeline([], edl.source_duration)
    by_idx = {w.idx: w for w in edl.words}
    cuts = cut_times(edl, apply_cuts=apply_cuts, duration=duration)

    # (приоритет, секунда, вид)
    wanted: list[tuple[int, float, str]] = []
    sheet = getattr(edl, "visuals", None)
    for item in (sheet.transitions if sheet is not None else []):
        word = by_idx.get(item.word_idx)
        window = timeline.word_window(word) if word is not None else None
        if window is None:
            continue
        at = window[0]
        near = min(cuts, key=lambda t: abs(t - at), default=None)
        if near is not None and abs(near - at) <= CUT_SNAP:
            wanted.append((0, near, item.kind))
        else:
            wanted.append((2, at, item.kind))
    wanted += [(1, at, "punch") for at in _shouts(plan)]

    guards = _scene_guards(plan)
    taken: list[tuple[float, str]] = []
    for _, at, kind in sorted(wanted):
        at = round(at * fps) / fps
        if at < TRANSITION_HEAD or at + length > duration - TRANSITION_TAIL:
            continue
        if any(lo < at + length and at < hi for lo, hi in guards):
            continue
        if any(abs(at - other) < TRANSITION_MIN_GAP for other, _ in taken):
            continue
        if kind in MOVING_KINDS and _pinned(plan, at, at + length):
            kind = "flash"
        taken.append((at, kind))

    out = [{"at": round(at, 4), "frames": frames, "kind": kind} for at, kind in sorted(taken)]
    if out:
        log.info(
            "переходы: %d (%s), просили %d, склеек %d",
            len(out), ", ".join(c["kind"] for c in out), len(wanted), len(cuts),
        )
    return out


def transition_swishes(edl: EDL, kinds: Sequence[str] = MOVING_KINDS) -> list[int]:
    """Слова листа, переход на которых двигает кадр, — им положен звук.

    Звук ставит `audio_post.plan_sfx` ещё до сборки плана, поэтому здесь только
    слова; частоту звука держит его же правило (`SFX_MIN_GAP_S`).
    """
    sheet = getattr(edl, "visuals", None)
    if sheet is None:
        return []
    return [t.word_idx for t in sheet.transitions if t.kind in kinds]
