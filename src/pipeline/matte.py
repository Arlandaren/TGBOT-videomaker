"""Маска человека для заголовка за головой (`src/core/behind.py`).

Титр первых секунд может уйти ЗА голову: рендерер накладывает на блок титра
покадровую маску «не человек», и текст исчезает там, где стоит голова. Маску
считает этот модуль — моделью MODNet через onnxruntime, на CPU.

Считается она не на весь ролик, а только на кадры жизни титра (секунда с
хвостом, ~40 кадров): это и делает приём возможным на сервере без видеокарты.

Порядок: `measure_behind` решает, есть ли вообще что мерить (титр в плане, нет
картинки поверх, кадр не поделён), и запускает ДОЧЕРНИЙ процесс
(`python -m src.pipeline.matte`). Тот читает кадры, гонит модель, проверяет по
маске, что приём выглядит хорошо (`behind.verdict`), подбирает место титра
(`behind.pick_shift`) и пишет маски. Отдельный процесс — это жёсткий срок и
память, которая возвращается целиком: у воркера 4 ГБ на всё.

Ни одна функция верхнего уровня не бросает исключений: любой сбой означает
обычный титр, а не ролик без титра и тем более не упавшую задачу.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from src.core.behind import (
    BehindDecision,
    Coverage,
    TitleBoxes,
    pick_shift,
    span_frames,
    title_boxes,
    verdict,
)
from src.core.config import Settings, settings as default_settings
from src.core.edl import EDL
from src.core.geometry import Rect
from src.core.preset import Preset, load_preset
from src.core.proc import ProcError, deadline
from src.core.render_plan import build_plan
from src.core.track import FaceTrack
from src.pipeline.probe import probe

log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "assets" / "models"
MODEL = MODELS_DIR / "modnet_photographic_portrait_matting.onnx"
# Файл, под который подобраны пороги `behind`. Другой файл — другие маски.
MODEL_SHA256 = "07c308cf0fc7e6e8b2065a12ed7fc07e1de8febb7dc7839d7b7f15dd66584df9"
# Растр модели, ширина и высота: стороны кратны 32, пропорция — кадр 9:16.
# Замер на сервере (3 ядра): 0.25 с на кадр и 290 МБ; край по волосам при этом
# неотличим от растра 576×1024, который вдвое медленнее.
MODEL_SIZE = (384, 672)
# Маска пишется в половину кадра: мягкий край даёт билинейное растяжение в
# браузере, а файл выходит в четыре раза легче.
MASK_SCALE = 0.5
# На сколько титр можно сдвинуть по вертикали, доли высоты кадра.
SHIFTS = tuple(round(-0.06 + 0.01 * i, 2) for i in range(13))
# Трек лица на кадрах титра обязан быть уверенным: без головы прятать не за что.
FACE_MIN_CONF = 0.5
DECODE_TIMEOUT = 60.0

Estimator = Callable[[np.ndarray], np.ndarray]


# --------------------------------------------------------------------------- кадры


def decode_span(
    video: str | Path, first: int, last: int, size: tuple[int, int], *, threads: int = 0
) -> np.ndarray:
    """Кадры `first..last` включительно, ужатые до `size`, rgb24: `[n, h, w, 3]`.

    Номера — кадры ФАЙЛА по порядку (`select` по `n`), а не время: маска обязана
    лечь на тот же кадр, что нарисует рендерер. Декодирование обрывается сразу
    после последнего нужного кадра.
    """
    width, height = size
    count = last - first + 1
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error"]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    cmd += [
        "-i", str(video),
        "-vf", rf"select='between(n\,{first}\,{last})',scale={width}:{height}:flags=area",
        "-vsync", "0",
        "-frames:v", str(count),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with deadline(proc, DECODE_TIMEOUT):
            assert proc.stdout is not None
            raw = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
    frame = width * height * 3
    got = len(raw) // frame
    return np.frombuffer(raw[: got * frame], dtype=np.uint8).reshape(got, height, width, 3)


# --------------------------------------------------------------------------- модель


def make_estimator(model: str | Path = MODEL, *, threads: int = 3) -> Estimator:
    """Модель как функция «кадры → маски человека» (`[n, h, w]`, 0..1)."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, threads)
    options.inter_op_num_threads = 1
    # Арена держит память до конца процесса; он короткий, но у воркера 4 ГБ на всё.
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name

    def estimate(frames: np.ndarray) -> np.ndarray:
        out = np.empty(frames.shape[:3], dtype=np.float32)
        for i, frame in enumerate(frames):
            x = ((frame.astype(np.float32) / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
            out[i] = session.run(None, {name: x})[0][0, 0]
        return np.clip(out, 0.0, 1.0)

    return estimate


def refine(mattes: np.ndarray) -> np.ndarray:
    """Край маски: чуть внутрь и без «кипения» между кадрами.

    Текст поверх пары выбившихся волосков не виден никому; кайма цвета фона,
    съедающая букву, видна сразу. Поэтому маска человека сжимается на пиксель и
    слегка размывается. Медиана по трём соседним кадрам гасит дрожание края:
    модель кадры между собой не связывает.
    """
    import cv2

    kernel = np.ones((3, 3), np.uint8)
    spatial = np.stack(
        [cv2.GaussianBlur(cv2.erode(m, kernel), (0, 0), 0.7) for m in mattes]
    ).astype(np.float32)
    if len(spatial) < 3:
        return spatial
    padded = np.concatenate([spatial[:1], spatial, spatial[-1:]])
    return np.median(np.stack([padded[:-2], padded[1:-1], padded[2:]]), axis=0).astype(np.float32)


# --------------------------------------------------------------------------- замер


def _mean(matte: np.ndarray, rect: Rect, frame: tuple[int, int]) -> float:
    """Средняя маска в прямоугольнике кадра. Мимо кадра — ноль: там человека нет."""
    h, w = matte.shape
    sx, sy = w / frame[0], h / frame[1]
    x0, x1 = int(round(rect.x * sx)), int(round((rect.x + rect.width) * sx))
    y0, y1 = int(round(rect.y * sy)), int(round((rect.y + rect.height) * sy))
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(matte[y0:y1, x0:x1].mean())


def coverage(
    matte: np.ndarray, boxes: TitleBoxes, frame: tuple[int, int], dy: float = 0.0
) -> Coverage:
    """Что человек закрывает на этом кадре, если титр сдвинуть на `dy` пикселей."""
    big = Rect(boxes.big.x, boxes.big.y + dy, boxes.big.width, boxes.big.height)
    top = Rect(big.x, big.y, big.width, big.height * 0.45)
    column = big.width / boxes.letters
    letters = [
        _mean(matte, Rect(big.x + i * column, big.y, column, big.height), frame)
        for i in range(boxes.letters)
    ]
    small = 0.0
    if boxes.small is not None:
        s = boxes.small
        small = _mean(matte, Rect(s.x, s.y + dy, s.width, s.height), frame)

    h, w = matte.shape
    sx, sy = w / frame[0], h / frame[1]
    x0, x1 = max(0, int(big.x * sx)), min(w, int((big.x + big.width) * sx) + 1)
    y0, y1 = max(0, int(big.y * sy)), min(h, int((big.y + big.height) * sy) + 1)
    band = matte[y0:y1, x0:x1]
    soft = float(((band > 0.1) & (band < 0.9)).mean()) if band.size else 0.0
    return Coverage(
        big=_mean(matte, big, frame),
        big_top=_mean(matte, top, frame),
        letter=max(letters, default=0.0),
        small=small,
        soft=soft,
    )


def write_masks(
    mattes: np.ndarray, out_dir: Path, frames: Sequence[int], size: tuple[int, int]
) -> None:
    """Маски «не человек» по файлу на кадр: `NNNNN.png`, альфа = 1 − человек.

    Имя — номер кадра выходного ролика, пять цифр: так его собирает рендерер
    (`Behind.tsx`). Альфа, а не яркость: CSS `mask-image` по умолчанию режет
    по альфе.
    """
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    for n, matte in zip(frames, mattes):
        alpha = Image.fromarray(np.uint8(np.round((1.0 - matte) * 255))).resize(
            size, Image.BILINEAR
        )
        mask = Image.new("LA", size, (255, 255))
        mask.putalpha(alpha)
        mask.save(out_dir / f"{n:05d}.png", optimize=False)


# --------------------------------------------------------------------------- дочерний процесс


def run_request(request: dict[str, Any], *, estimator: Estimator | None = None) -> BehindDecision:
    """Посчитать маски и решить, уходит ли титр за голову. Работа дочернего процесса."""
    t0 = time.perf_counter()
    first, last, step = int(request["first"]), int(request["last"]), int(request.get("step", 1))
    frame = (int(request["frame"][0]), int(request["frame"][1]))
    out_dir = Path(request["out_dir"])
    boxes = TitleBoxes(
        big=Rect(*request["big"]),
        small=Rect(*request["small"]) if request.get("small") else None,
        letters=int(request["letters"]),
    )

    def refuse(reason: str) -> BehindDecision:
        shutil.rmtree(out_dir, ignore_errors=True)
        return BehindDecision(
            first=first, last=last, reason=reason, seconds=round(time.perf_counter() - t0, 2)
        )

    size = tuple(request.get("size") or MODEL_SIZE)
    frames = decode_span(
        request["video"], first, last, (int(size[0]), int(size[1])),
        threads=int(request.get("ffmpeg_threads", 0)),
    )
    if len(frames) < last - first + 1:
        return refuse(f"видео кончилось раньше титра: {len(frames)} кадров из {last - first + 1}")

    estimate = estimator or make_estimator(
        request.get("model") or MODEL, threads=int(request.get("threads", 3))
    )
    # На 60 fps модель идёт через кадр: маска соседнего кадра — та же с точностью,
    # которой не видно, а время вдвое меньше.
    picked = frames[::step]
    mattes = refine(estimate(picked))

    table = {
        dy: [coverage(m, boxes, frame, dy) for m in mattes]
        for dy in (float(s) for s in request["shifts"])
    }
    dy = pick_shift(table)
    if dy is None:
        at_rest = min(table, key=abs) if table else 0.0
        return refuse(verdict(table.get(at_rest, [])) or "нет подходящего места для титра")

    numbers = list(range(first, last + 1))
    full = np.repeat(mattes, step, axis=0)[: len(numbers)]
    mask_size = (max(2, round(frame[0] * MASK_SCALE)), max(2, round(frame[1] * MASK_SCALE)))
    shutil.rmtree(out_dir, ignore_errors=True)
    write_masks(full, out_dir, numbers, mask_size)
    covers = sorted(c.big for c in table[dy])
    return BehindDecision(
        dir=str(out_dir),
        first=first,
        last=last,
        dy=dy,
        cover=round(covers[len(covers) // 2], 3),
        seconds=round(time.perf_counter() - t0, 2),
    )


def main() -> None:
    """`python -m src.pipeline.matte`: запрос JSON на stdin → решение JSON на stdout."""
    request = json.loads(sys.stdin.read())
    try:
        decision = run_request(request)
    except Exception as e:  # noqa: BLE001 — сбой маски не должен стоить ролика
        shutil.rmtree(request.get("out_dir", ""), ignore_errors=True)
        decision = BehindDecision(reason=f"сбой расчёта маски: {e}")
    sys.stdout.write(decision.model_dump_json())


def _in_child(request: dict[str, Any], timeout: float) -> BehindDecision:
    """Запустить расчёт отдельным процессом с жёстким сроком."""
    out = subprocess.run(
        [sys.executable, "-m", "src.pipeline.matte"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if out.returncode != 0:
        raise ProcError(f"расчёт маски упал: {out.stderr.strip()[-400:]}")
    return BehindDecision.model_validate_json(out.stdout)


# --------------------------------------------------------------------------- вход


def _xywh(rect: Rect) -> list[float]:
    return [rect.x, rect.y, rect.width, rect.height]


def model_ok(model: Path = MODEL) -> bool:
    """На месте ли файл модели — тот самый, под который подобраны пороги."""
    try:
        return hashlib.sha256(model.read_bytes()).hexdigest() == MODEL_SHA256
    except OSError:
        return False


def measure_behind(
    edl: EDL,
    video: str | Path,
    work: str | Path,
    *,
    preset: Preset | None = None,
    apply_cuts: bool = True,
    track: FaceTrack | None = None,
    cfg: Settings | None = None,
    runner: Callable[[dict[str, Any], float], BehindDecision] | None = None,
) -> BehindDecision | None:
    """Решение «титр за головой» для этого ролика или `None`, если мерить нечего.

    `None` и решение с причиной отказа значат одно и то же для рендера — титр
    рисуется как раньше; разница только в том, запускалась ли модель. Маски
    лежат в `work/matte/title`, старые перед расчётом стираются.
    """
    cfg = cfg or default_settings
    out_dir = Path(work) / "matte" / "title"
    shutil.rmtree(out_dir.parent, ignore_errors=True)
    try:
        return _measure(edl, Path(video), out_dir, preset, apply_cuts, track, cfg, runner)
    except Exception as e:  # noqa: BLE001 — приём важен, но ролик важнее
        log.warning("титр за головой пропущен: %s", e)
        shutil.rmtree(out_dir.parent, ignore_errors=True)
        return None


def _measure(
    edl: EDL,
    video: Path,
    out_dir: Path,
    preset: Preset | None,
    apply_cuts: bool,
    track: FaceTrack | None,
    cfg: Settings,
    runner: Callable[[dict[str, Any], float], BehindDecision] | None,
) -> BehindDecision | None:
    def skip(why: str) -> None:
        log.info("титр за головой: не меряем — %s", why)
        return None

    if not cfg.matte_enabled:
        return skip("выключено настройкой")
    theme = preset or load_preset(edl.preset)
    card = theme.caption.title_card
    if not (card.enabled and card.behind) or card.panel:
        return skip("тема приём не использует")
    if not (edl.title or "").strip():
        return skip("у ролика нет заголовка")
    if edl.split_until is not None:
        return skip("кадр поделён на два видео")
    if track is None:
        return skip("лицо в кадре не найдено")
    if runner is None and not model_ok():
        return skip("нет файла модели")

    info = probe(video)
    width, height = info.width, info.height
    fps = int(round(info.fps)) if info.fps and info.fps > 0 else cfg.render_fps
    if width <= 0 or height <= 0:
        return skip("у видео не читается размер кадра")

    plan = build_plan(
        edl, str(video), fps=fps, width=width, height=height, preset=theme,
        apply_cuts=apply_cuts, cfg=cfg, track=track,
    )
    block = next((b for b in plan.blocks if b.title_card), None)
    if block is None or block.rect is None:
        return skip("титра в этом ролике нет")
    if any(pic.start < block.end and pic.end > block.start for pic in plan.broll):
        return skip("во время титра идёт картинка")

    scaled = theme.scaled(height)
    first, last = span_frames(block.start, block.end, fps, scaled)
    last = min(last, max(first, round(plan.duration * fps) - 1))
    for f in range(first, last + 1):
        point = track.at(f)
        if point is None or point.conf < FACE_MIN_CONF:
            return skip(f"на кадре {f} лицо не найдено")

    boxes = title_boxes(block, scaled.caption.title_card.preset(scaled), (width, height))
    request = {
        "video": str(video),
        "first": first,
        "last": last,
        "step": 2 if fps > 40 else 1,
        "frame": [width, height],
        "big": _xywh(boxes.big),
        "small": _xywh(boxes.small) if boxes.small else None,
        "letters": boxes.letters,
        "shifts": [round(s * height, 2) for s in SHIFTS],
        "out_dir": str(out_dir),
        "threads": cfg.matte_threads,
        "ffmpeg_threads": cfg.ffmpeg_threads,
    }
    decision = (runner or _in_child)(request, cfg.matte_timeout)
    if decision.on:
        log.info(
            "титр за головой: кадры %d-%d, сдвиг %+.0f px, закрыто %.0f %%, %.1f с",
            decision.first, decision.last, decision.dy, (decision.cover or 0) * 100,
            decision.seconds or 0.0,
        )
    else:
        log.info("титр за головой: обычный титр — %s", decision.reason)
        shutil.rmtree(out_dir.parent, ignore_errors=True)
    return decision


if __name__ == "__main__":
    main()
