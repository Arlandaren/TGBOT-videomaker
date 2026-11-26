"""Готовый ролик в `output_fps` (по умолчанию 60) кадров в секунду.

Шаг стоит между кадрированием и рендером. Дальше всё берёт частоту отсюда:
замер яркости, маска «за головой», план и Remotion. Поэтому кроп, камера,
субтитры и графика двигаются с шагом в 1/60 секунды. Трек лица кроп сразу
снимает на этой же сетке (`target_fps`), иначе камера не стала бы его трогать
(`camera.shoot`: трек с чужой частотой остаётся как есть).

Почему не в нарезке: там кадр ещё в исходном размере (4K у телефона), а
дорисовка кадров стоит пропорционально площади. После кропа это 1080×1920.

Съёмку с частотой ниже нужной добирают двумя способами (`fps_interpolation`):

- "motion": промежуточный кадр дорисовывается по движению (`minterpolate`,
  компенсация движения блоками). Получаются честные 60. Фильтр однопоточный и
  медленный: на сервере (6 ядер EPYC) 22 с на секунду ролика в один поток.
  Поэтому ролик режется на куски по числу ядер, они считаются параллельно и
  склеиваются без перекодирования (6.5 с на секунду ролика). Склейки в самом
  видео фильтр замечает сам (`scd=fdiff`): на них он повторяет кадр, а не
  смешивает два плана.
- "repeat": кадр повторяется. Быстро, но съёмка остаётся в своей частоте.

Если дорисовка не удалась, ролик всё равно едет: повтором кадров. Частота у
него та же, поэтому трек лица, снятый на сетке `target_fps`, остаётся верным.
"""

from __future__ import annotations

import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from src.core.config import Settings, settings as default_settings
from src.core.proc import INTERMEDIATE_PRESET, ffmpeg
from src.pipeline import probe as probe_mod
from src.pipeline.probe import MediaInfo

log = logging.getLogger(__name__)

# Короче куска нет смысла резать: запуск ffmpeg и разгон фильтра съедают выигрыш.
MIN_CHUNK_SECONDS = 2.0
# Сколько лишних секунд кусок читает после своего конца: последнему кадру куска
# фильтру нужен следующий кадр исходника, иначе он повторит предпоследний.
CHUNK_TAIL = 0.2
# Промежуточный файл: рендер его всё равно пережмёт, поэтому кодек быстрый, а
# качество как у нарезки (`cut.py`). Ниже crf не берётся: на сервере тесно с
# диском, а файл вдвое длиннее по кадрам.
X264 = ["-c:v", "libx264", "-preset", INTERMEDIATE_PRESET, "-crf", "18", "-pix_fmt", "yuv420p"]
# Блоки 16 px, двунаправленный поиск, перекрытие блоков с адаптацией: так у
# движущейся руки нет «сетки» на краях. Склейку ловит scd.
MOTION = "mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:scd=fdiff"


def target_fps(src_fps: float, cfg: Settings | None = None) -> int:
    """Частота готового ролика для исходника с частотой `src_fps`."""
    cfg = cfg or default_settings
    if cfg.output_fps > 0:
        return cfg.output_fps
    return int(round(src_fps)) if src_fps and src_fps > 0 else cfg.render_fps


@dataclass(frozen=True)
class Chunk:
    """Кусок для параллельной дорисовки: с какого кадра выхода и сколько кадров."""

    first: int
    frames: int

    def start(self, fps: int) -> float:
        return self.first / fps


def chunks(total_frames: int, fps: int, workers: int) -> list[Chunk]:
    """Делит `total_frames` кадров выхода на куски поровну, не короче MIN_CHUNK_SECONDS.

    Куски идут встык и без пропусков: склейка без перекодирования требует, чтобы
    сумма кусков была ровно длиной ролика.
    """
    if total_frames <= 0:
        return []
    by_length = max(1, int(total_frames / (MIN_CHUNK_SECONDS * fps)))
    n = max(1, min(workers, by_length))
    bounds = [round(i * total_frames / n) for i in range(n + 1)]
    return [Chunk(a, b - a) for a, b in zip(bounds, bounds[1:]) if b > a]


# Память одного куска дорисовки на кадре 1080×1920 (замер на сервере
# 2026-11-26: 887 МБ у процесса ffmpeg). Шесть кусков по числу ядер — это
# 5.3 ГБ при лимите воркера 4 ГБ: систему убивал OOM, и ролик не доезжал.
CHUNK_MEMORY = 900 * 1024 * 1024
# Доля свободной памяти cgroup, которую можно отдать кускам: остальное — запас
# на сам воркер, его модель распознавания и кодек.
MEMORY_SHARE = 0.8


def cgroup_budget(
    proc_cgroup: Path = Path("/proc/self/cgroup"), root: Path = Path("/sys/fs/cgroup"),
) -> tuple[float | None, int | None]:
    """Сколько ядер и байт памяти разрешает процессу его cgroup (v2).

    `os.cpu_count()` видит ядра машины, а не квоту службы: на сервере их шесть,
    а воркеру отданы три (`CPUQuota=300%`) и 4 ГБ (`MemoryMax`). Лимит может
    стоять на любом уровне выше, поэтому берётся самый тесный по пути к корню.
    `None` — ограничения нет или cgroup не читается (Windows, macOS, cgroup v1).
    """
    try:
        rel = next(
            line[3:] for line in proc_cgroup.read_text().splitlines() if line.startswith("0::")
        )
    except (OSError, StopIteration):
        return None, None
    cpus: float | None = None
    memory: int | None = None
    node = root / rel.strip().lstrip("/")
    while True:
        try:
            quota, period = (node / "cpu.max").read_text().split()[:2]
            if quota != "max" and float(period) > 0:
                share = float(quota) / float(period)
                cpus = share if cpus is None else min(cpus, share)
        except (OSError, ValueError):
            pass
        try:
            limit = (node / "memory.max").read_text().strip()
            if limit != "max":
                free = int(limit) - int((node / "memory.current").read_text().strip())
                memory = free if memory is None else min(memory, free)
        except (OSError, ValueError):
            pass
        if node == root or root not in node.parents:
            break
        node = node.parent
    return cpus, memory


def _workers(cfg: Settings) -> int:
    """Сколько кусков дорисовывать разом: не больше ядер и памяти, что нам отданы."""
    if cfg.fps_workers > 0:
        return cfg.fps_workers
    n = max(1, os.cpu_count() or 1)
    cpus, memory = cgroup_budget()
    if cpus is not None:
        n = min(n, max(1, int(cpus)))
    if memory is not None:
        n = min(n, max(1, int(memory * MEMORY_SHARE // CHUNK_MEMORY)))
    return n


def _plain(src: Path, dst: Path, info: MediaInfo, fps: int) -> Path:
    """Частота фильтром `fps`: повтор кадров вверх, выброс вниз. Звук как есть."""
    args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-vf", f"fps={fps}", *X264]
    if info.has_audio:
        args += ["-c:a", "copy"]
    ffmpeg([*args, "-movflags", "+faststart", str(dst)], timeout=3600)
    return dst


def _motion_chunk(src: Path, dst: Path, chunk: Chunk, fps: int) -> Path:
    start = chunk.start(fps)
    seconds = chunk.frames / fps + CHUNK_TAIL
    ffmpeg(
        [
            "-ss", f"{start:.6f}", "-t", f"{seconds:.6f}", "-i", str(src), "-an",
            # `tpad`: у последнего куска после конца ролика кадров нет, и фильтр
            # не выдал бы последние 2-3 кадра. Удержание даёт ему, на что
            # опереться; лишнее срезает `-frames:v`.
            "-vf", f"tpad=stop_mode=clone:stop_duration={CHUNK_TAIL},minterpolate=fps={fps}:{MOTION}",
            "-frames:v", str(chunk.frames), "-r", str(fps),
            # Один поток на кусок: параллельность даёт число кусков, а лишние
            # потоки кодека только дерутся с соседями за ядра.
            "-filter_threads", "1", "-threads", "1",
            *X264, str(dst),
        ],
        # minterpolate на сервере идёт ~22 с на секунду ролика; втрое с запасом.
        timeout=300 + 70 * seconds,
    )
    return dst


def _motion(src: Path, dst: Path, info: MediaInfo, fps: int, work: Path, cfg: Settings) -> Path:
    total = max(1, int(round(info.duration * fps)))
    parts = chunks(total, fps, _workers(cfg))
    paths = [work / f"fps-{i:02d}.mp4" for i in range(len(parts))]
    with ThreadPoolExecutor(max_workers=len(parts)) as pool:
        # `list` — чтобы исключение из любого куска дошло сюда, а не потерялось.
        list(pool.map(lambda a: _motion_chunk(src, *a, fps), zip(paths, parts)))

    listing = work / "fps-list.txt"
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in paths), encoding="utf-8"
    )
    args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    if info.has_audio:
        args += ["-i", str(src), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
    else:
        args += ["-map", "0:v:0"]
    ffmpeg(
        [*args, "-c:v", "copy", "-t", f"{info.duration:.6f}", "-movflags", "+faststart", str(dst)],
        timeout=1800,
    )
    for p in [*paths, listing]:
        p.unlink(missing_ok=True)
    return dst


def to_output_fps(
    src: str | Path, dst: str | Path, *, info: MediaInfo | None = None,
    work_dir: str | Path | None = None, cfg: Settings | None = None,
) -> Path:
    """Видео в частоте `target_fps`. Уже в ней — возвращается `src` без прохода.

    Не бросает: если не вышло ни дорисовать, ни повторить кадры, возвращается
    `src` в своей частоте. Ролик тогда выйдет как раньше, только камера не
    тронет трек лица (он снят на сетке `target_fps`) — это лучше, чем отказ.
    """
    cfg = cfg or default_settings
    src, dst = Path(src), Path(dst)
    work = Path(work_dir) if work_dir is not None else dst.parent
    work.mkdir(parents=True, exist_ok=True)
    info = info or probe_mod.probe(src)
    fps = target_fps(info.fps, cfg)
    if info.fps > 0 and math.isclose(info.fps, fps, abs_tol=0.01):
        return src

    if cfg.fps_interpolation == "motion" and info.fps < fps:
        try:
            out = _motion(src, dst, info, fps, work, cfg)
            if probe_mod.is_playable(out):
                log.info("кадры: %.3g → %d к/с, дорисовка по движению", info.fps, fps)
                return out
            log.warning("дорисовка кадров дала битый файл — повторяю кадры")
        except Exception as e:  # noqa: BLE001 — ролик важнее плавности
            log.warning("дорисовка кадров не удалась (%s) — повторяю кадры", e)
    try:
        out = _plain(src, dst, info, fps)
        if probe_mod.is_playable(out):
            log.info("кадры: %.3g → %d к/с, %s", info.fps, fps, "повтор" if info.fps < fps else "выброс")
            return out
        log.warning("перевод в %d к/с дал битый файл — ролик в исходной частоте", fps)
    except Exception as e:  # noqa: BLE001
        log.warning("перевод в %d к/с не удался (%s) — ролик в исходной частоте", fps, e)
    return src
