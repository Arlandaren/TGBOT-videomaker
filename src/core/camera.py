"""Камера рендера: медленный наезд внутри плана и укрупнение на резе.

Что было. Движение в кадре делалось только сменой крупности на резе, и делал
его кроп (`pipeline/crop.py`). Между резами кадр стоял намертво, а на
вертикальном 1080p — самом частом исходнике — не менялся вовсе: окну кропа там
сужаться некуда (`Crop.max_scale`), и чередование планов оставалось в листе.

Что здесь. План (кусок одной крупности) получает масштаб видео-слоя:

* начальный — 1.0 или `punch_scale`, если план крупный, а кроп укрупнить не смог;
* конечный — начальный плюс медленный наезд за жизнь плана.

Масштаб меняется скачком только на границе планов, то есть на резе: правило
«крупность меняется на резе» (QUALITY_PLAN Ф6) не тронуто. Внутри плана он
линеен по времени — никаких пружин и синусов по номеру кадра, на которых
сгорела прежняя камера (AUDIT §8.2).

Рендерер решений не принимает (§0): он получает готовые планы и считает одну
линейную интерполяцию. Масштабируется только видео; субтитры и вставки стоят
снаружи и не двигаются.

Границы планов считаются той же формулой, что у кропа (`core/framing.py`), и
подгоняются под настоящую длительность файла так же: иначе сброс масштаба
пришёлся бы не на рез, а на кадр-другой внутрь соседнего плана.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Sequence

from src.core.edl import EDL
from src.core.framing import framing_spans, normalize_spans
from src.core.preset import Preset
from src.core.track import FaceTrack, TrackPoint

log = logging.getLogger(__name__)

# Точка, вокруг которой растёт кадр, когда лица нет: чуть выше центра — там у
# говорящей головы лицо и стоит.
DEFAULT_ORIGIN = (0.5, 0.42)
# Наезд идёт в ЛОБ, а не в центр лица: точка масштаба остаётся на месте, всё
# остальное от неё разъезжается, и с центром в переносице макушка уезжала бы
# вверх за кадр. Доля высоты лица вверх от его центра.
ORIGIN_ABOVE_FACE = 0.5
# Куда точка масштаба может уйти, доли кадра. Лицо у самого края — это ошибка
# трека или человек, выходящий из кадра; наезжать туда незачем.
ORIGIN_X = (0.25, 0.75)
ORIGIN_Y = (0.12, 0.6)
MIN_CONF = 0.5
# Меньше этого укрупнение не читается как смена плана — только как дрожь на резе.
MIN_PUNCH = 1.03


@dataclass(slots=True)
class PlanShot:
    """План камеры: масштаб видео от `scale_from` до `scale_to`, линейно.

    Время — секунды выходного таймлайна, уже на кадровой сетке. `ox`/`oy` —
    точка в пикселях кадра, которая при масштабировании остаётся на месте.
    """

    start: float
    end: float
    scale_from: float
    scale_to: float
    ox: float
    oy: float


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _origin(track: FaceTrack | None, start: float, end: float) -> tuple[float, float]:
    """Точка масштаба плана в долях кадра: лоб говорящего, медианой по плану.

    Одна на весь план, а не по кадрам: точка, гуляющая за головой, превратила бы
    ровный наезд в качку.
    """
    if track is None or not track.points:
        return DEFAULT_ORIGIN
    lo, hi = round(start * track.fps), round(end * track.fps)
    seen = [p for p in track.points if lo <= p.f < hi and p.conf >= MIN_CONF]
    if not seen:
        return DEFAULT_ORIGIN
    return (
        _clamp(median(p.cx for p in seen), *ORIGIN_X),
        _clamp(median(p.cy - ORIGIN_ABOVE_FACE * p.h for p in seen), *ORIGIN_Y),
    )


def _crop_zoom(track: FaceTrack | None, start: float, end: float) -> float | None:
    """Во сколько раз кроп уже укрупнил этот план. `None` — неизвестно."""
    if track is None or not track.windows:
        return None
    mid = (start + end) / 2
    for w in track.windows:
        if w.start <= mid < w.end:
            return w.zoom
    return track.windows[-1].zoom


def plan_camera(
    edl: EDL,
    *,
    preset: Preset,
    frame: tuple[int, int],
    fps: int,
    apply_cuts: bool = True,
    track: FaceTrack | None = None,
    video_duration: float | None = None,
) -> list[PlanShot]:
    """Лист → планы камеры. Пусто — камеры нет, видео стоит как стояло.

    Камеры нет, и каждая причина — правило:

    * её выключила тема (`camera.enabled`) или пользователь (`EDL.camera`);
    * кадр поделён на два видео (`EDL.split_until`): масштабировать склейку из
      двух половин значит двигать шов.

    `video_duration` — настоящая длительность файла, который увидит рендер. Без
    неё границы планов остаются точными по листу, а файл на кадр-другой длиннее
    (`framing._fit_spans`).
    """
    cam = preset.camera
    if not cam.enabled or not edl.camera or edl.split_until is not None:
        return []
    width, height = frame
    total = video_duration if video_duration and video_duration > 0 else (
        edl.out_duration if apply_cuts else edl.source_duration
    )
    spans = normalize_spans(framing_spans(edl, applied_cuts=apply_cuts), total)

    shots: list[PlanShot] = []
    for start, end, framing in spans:
        # На кадровую сетку: рендерер сравнивает номера кадров, и граница,
        # попавшая между кадрами, у Python и у него округлилась бы по-разному.
        a, b = round(start * fps) / fps, round(end * fps) / fps
        if b - a < 1 / fps:
            continue
        base = 1.0
        if framing == "tight":
            done = _crop_zoom(track, start, end)
            # Неизвестно, что сделал кроп, — не добавляем: два укрупнения
            # подряд хуже, чем ни одного.
            if done is not None:
                base = cam.punch_scale / done
                if base < MIN_PUNCH:
                    base = 1.0
        depth = min(cam.drift_max, cam.drift_per_s * (b - a))
        to = base * (1.0 + depth)
        if to <= 1.0 + 1e-6:
            continue
        ox, oy = _origin(track, a, b)
        shots.append(
            PlanShot(
                start=round(a, 4),
                end=round(b, 4),
                scale_from=round(base, 4),
                scale_to=round(to, 4),
                ox=round(ox * width, 2),
                oy=round(oy * height, 2),
            )
        )
    if shots:
        log.info(
            "камера: планов %d, масштаб %.3f..%.3f",
            len(shots), min(s.scale_from for s in shots), max(s.scale_to for s in shots),
        )
    return shots


def state_at(shots: Sequence[PlanShot], frame_no: int, fps: int) -> tuple[float, float, float]:
    """Масштаб и точка масштаба на кадре: `(scale, ox, oy)`.

    Зеркало `cameraAt` в `render/src/layers/Camera.ts` — та же арифметика на
    номерах кадров. До первого плана и между планами камеры нет (масштаб 1);
    после последнего держится его конец: файл бывает на кадр длиннее листа.
    """
    last: PlanShot | None = None
    for shot in shots:
        first, stop = round(shot.start * fps), round(shot.end * fps)
        if frame_no < first:
            break
        last = shot
        if frame_no < stop:
            p = (frame_no - first) / max(1, stop - first)
            return shot.scale_from + (shot.scale_to - shot.scale_from) * p, shot.ox, shot.oy
    if last is not None and last is shots[-1]:
        return last.scale_to, last.ox, last.oy
    return 1.0, 0.0, 0.0


def zoom_track(
    track: FaceTrack | None, shots: Sequence[PlanShot], frame: tuple[int, int]
) -> FaceTrack | None:
    """Трек лица таким, каким его увидит зритель — уже после камеры.

    Трек снят с файла до рендера, а субтитры ставятся поверх кадра после него:
    привязка к голове и запрет закрывать лицо обязаны смотреть на лицо там, где
    оно окажется. Без планов трек возвращается как есть.
    """
    if track is None or not shots or not track.points:
        return track
    width, height = frame
    points: list[TrackPoint] = []
    for p in track.points:
        scale, ox, oy = state_at(shots, p.f, track.fps)
        if scale == 1.0:
            points.append(p)
            continue
        points.append(
            TrackPoint(
                f=p.f,
                cx=_clamp((ox + scale * (p.cx * width - ox)) / width, -1.0, 2.0),
                cy=_clamp((oy + scale * (p.cy * height - oy)) / height, -1.0, 2.0),
                h=_clamp(p.h * scale, 0.0, 4.0),
                conf=p.conf,
            )
        )
    return track.model_copy(update={"points": points})


def shoot(
    edl: EDL,
    *,
    preset: Preset,
    frame: tuple[int, int],
    fps: int,
    apply_cuts: bool = True,
    track: FaceTrack | None = None,
    video_duration: float | None = None,
) -> tuple[list[PlanShot], FaceTrack | None]:
    """Планы камеры и трек лица под них — одним вызовом для сборки ролика.

    Трек приводится к высоте пресета кадра ровно так, как его ждёт `build_plan`:
    тот получает лицо уже там, где оно будет после камеры. Трек с другой
    частотой кадров, чем рендер, остаётся как есть — пересчитывать его по чужой
    сетке значит сдвинуть лицо во времени.
    """
    shots = plan_camera(
        edl, preset=preset, frame=frame, fps=fps, apply_cuts=apply_cuts,
        track=track, video_duration=video_duration,
    )
    if track is not None and track.fps != fps:
        return shots, track
    return shots, zoom_track(track, shots, frame)


def to_meta(shots: Sequence[PlanShot]) -> list[dict[str, Any]]:
    """Планы в том виде, в каком они едут рендереру (`plan.meta["camera"]`)."""
    return [asdict(s) for s in shots]


def filmed_plan(
    edl: EDL,
    video_path: str,
    *,
    backend: str,
    video_duration: float | None = None,
    **plan_args: Any,
) -> Any:
    """`build_plan` вместе с камерой: единый вход для всех сборок ролика.

    Порядок обязателен: сначала камера, потом план. План ставит субтитры по
    треку лица, а лицо после камеры стоит уже не там, где его снял кроп.

    Быстрый путь (`ass`) камеру не рисует вовсе — там видео идёт через ffmpeg
    как есть, — поэтому и трек ему остаётся прежний.
    """
    from src.core.overlay_models import Visuals
    from src.core.overlays import dressed_plan
    from src.core.render_plan import build_plan

    track = plan_args.pop("track", None)
    shots: list[PlanShot] = []
    if backend != "ass" and plan_args.get("preset") is not None:
        shots, track = shoot(
            edl,
            preset=plan_args["preset"],
            frame=(plan_args["width"], plan_args["height"]),
            fps=plan_args["fps"],
            apply_cuts=plan_args.get("apply_cuts", True),
            track=track,
            video_duration=video_duration,
        )
    # Графика (`core/overlays.py`) ложится на готовый план: без `edl.visuals`
    # план тот же, что дал бы голый `build_plan`. В быстром пути (`ass`) её
    # рисовать некому.
    visuals = edl.visuals if backend != "ass" else Visuals()
    plan = dressed_plan(build_plan, edl, video_path, track=track, visuals=visuals, **plan_args)
    if shots and isinstance(getattr(plan, "meta", None), dict):
        plan.meta["camera"] = to_meta(shots)
    return plan
