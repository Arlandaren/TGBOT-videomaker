"""Сборка версии ролика из правленого листа (редактор сцен, `core/edit.py`).

Тот же путь, что у перерендера в другую тему (`worker.tasks.rerender_job`):
нарезка, звук, кроп, план, рендер — без распознавания и без LLM. Отличий три:
лист приходит уже правленый, прогресс идёт в задачу (версия — обычная задача
с полосой в приложении), и у вставок бывают свои картинки человека.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.core.camera import filmed_plan
from src.core.config import Settings
from src.core.edl import EDL
from src.core.options import JobOptions, apply_accent, apply_font, apply_grade, job_settings
from src.core.preset import load_preset
from src.core.track import load_track
from src.pipeline.asr import ASRResult
from src.pipeline.audio_post import build_audio
from src.pipeline.crop import autocrop, framing_spans
from src.pipeline.cut import apply_cuts
from src.pipeline.fps import to_output_fps
from src.pipeline.job import JobError, JobResult, _measure_behind, _measure_luma, render_share
from src.pipeline.probe import is_playable, probe
from src.pipeline.render import arender_remotion, render_ass
from src.pipeline.split import stage_split

log = logging.getLogger(__name__)

Progress = Callable[[str, float, str | None], Awaitable[None]]
# Своя картинка ужимается до этой стороны: карточка в кадре 1080 шире не бывает.
MEDIA_SIDE = 1600


def _normalise_picture(raw: Path, out: Path) -> float:
    """Картинка человека → JPEG разумного размера. Возвращает ширину/высоту.

    Поворот из EXIF применяется здесь: Chromium в рендере его учитывает не
    всегда, и фото с телефона ложилось бы набок.
    """
    from PIL import Image, ImageOps

    with Image.open(raw) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((MEDIA_SIDE, MEDIA_SIDE))
        img.save(out, "JPEG", quality=90)
        return img.width / img.height


async def stage_pictures(edl: EDL, work: Path, download: Callable[[str, Path], Awaitable[Any]]) -> int:
    """Довезти картинки вставок до рабочей папки. Возвращает, сколько осталось.

    Своя картинка (`asset_key`) скачивается из хранилища. Нарисованная раньше
    (`asset` — путь в общем кеше) остаётся, пока файл цел: кеш живёт две
    недели, и вставка без файла уронила бы рендер — такая вставка выпадает.
    """
    kept = []
    for n, item in enumerate(edl.broll):
        if item.asset_key:
            raw = work / f"picture-{n}.src"
            out = work / f"picture-{n}.jpg"
            try:
                await download(item.asset_key, raw)
                item.asset_aspect = round(await asyncio.to_thread(_normalise_picture, raw, out), 4)
                item.asset = str(out)
            except Exception as e:  # noqa: BLE001 — без картинки ролик всё равно едет
                log.warning("своя картинка %s не доехала: %s", item.asset_key, e)
                continue
            finally:
                raw.unlink(missing_ok=True)
        elif not item.asset or not Path(item.asset).is_file():
            log.info("вставка %r выпала: файла в кеше уже нет", item.query)
            continue
        kept.append(item)
    edl.broll = kept
    return len(kept)


async def render_edit(
    edl: EDL,
    src_path: Path,
    work: Path,
    *,
    options: JobOptions,
    cut_pauses: bool,
    cfg: Settings,
    top_path: Path | None = None,
    music_file: Path | None = None,
    progress: Progress | None = None,
) -> JobResult:
    """Правленый лист → готовый ролик. `cut_pauses` — применять ли резы листа."""
    timings: dict[str, float] = {}

    async def say(stage: str, share: float, key: str) -> None:
        if progress is not None:
            await progress(stage, share, key)

    def mark(stage: str, t0: float) -> None:
        timings[stage] = round(time.perf_counter() - t0, 2)

    # Тяжёлое — в рабочий поток: цикл событий обязан дышать, иначе воркер не
    # слышит SIGTERM от выкладки (то же, что в `process_video`).
    await say("Читаю файл", 0.03, "probe")
    info = await asyncio.to_thread(probe, src_path)
    # Галочки задачи (студийный звук, слежение, переходы, полоса, 60 к/с)
    # действуют и на версию: она обязана быть тем же роликом с правками.
    job_cfg = job_settings(cfg, options)
    style = apply_grade(
        apply_font(apply_accent(load_preset(edl.preset), options.accent), edl.font or options.font),
        options.grade,
    )

    await say("Режу паузы", 0.20, "cut")
    t = time.perf_counter()
    cut_path = work / "cut.mp4"
    if cut_pauses:
        await asyncio.to_thread(apply_cuts, edl, src_path, cut_path, info=info, cfg=job_cfg)
    else:
        await asyncio.to_thread(shutil.copy2, src_path, cut_path)
    if not await asyncio.to_thread(is_playable, cut_path):
        raise JobError("Нарезка оборвалась — файл повреждён.", "cut_broken")
    mark("cut", t)

    await say("Свожу звук", 0.35, "audio")
    t = time.perf_counter()
    audio_path: str | None = None
    if cfg.audio_post_enabled:
        try:
            track = await asyncio.to_thread(
                build_audio,
                edl, cut_path, work / "audio.wav", preset=style,
                apply_cuts=cut_pauses, sfx=options.sfx, cfg=job_cfg, music_file=music_file,
            )
            if track is not None:
                audio_path = str(track.path)
        except Exception as e:  # noqa: BLE001 — без звукового поста ролик всё равно едет
            log.warning("версия ролика: звуковой пост пропущен: %s", e)
    mark("audio", t)

    await say("Кадрирую 9:16", 0.45, "crop")
    t = time.perf_counter()
    framing = framing_spans(edl, applied_cuts=cut_pauses)
    if edl.split_until is not None:
        if top_path is None:
            raise JobError("Исходный файл потерялся. Пришлите видео ещё раз.", "source_lost")
        vertical = await asyncio.to_thread(
            stage_split,
            cut_path, top_path, work / "vertical.mp4",
            until=edl.split_until,
            out_duration=edl.out_duration if cut_pauses else info.duration,
            work_dir=work, cfg=job_cfg, preset=style, framing=framing,
            track_out=work / "track.json",
        )
    else:
        vertical = await asyncio.to_thread(
            autocrop,
            cut_path, work / "vertical.mp4", work_dir=work, cfg=job_cfg,
            preset=style, framing=framing, track_out=work / "track.json",
        )
    if not await asyncio.to_thread(is_playable, vertical):
        raise JobError("Кадрирование оборвалось — файл повреждён.", "crop_broken")
    vertical = await asyncio.to_thread(
        to_output_fps, vertical, work / "smooth.mp4",
        info=await asyncio.to_thread(probe, vertical), work_dir=work, cfg=job_cfg,
    )
    staged_info = await asyncio.to_thread(probe, vertical)
    mark("crop", t)

    # Яркость под субтитрами и маска под титр — заново: текст, его место и
    # размер могли поменяться, а замер прошлой версии был сделан под прежние.
    await asyncio.to_thread(_measure_luma, edl, vertical, preset=style, cut_pauses=cut_pauses, cfg=job_cfg)
    behind = await asyncio.to_thread(
        _measure_behind, edl, vertical, work, preset=style, cut_pauses=cut_pauses,
        backend=cfg.render_backend, cfg=job_cfg,
    )
    edl.camera = edl.camera and options.camera
    plan = filmed_plan(
        edl, str(vertical), backend=cfg.render_backend,
        video_duration=staged_info.duration, fps=int(round(staged_info.fps)),
        width=staged_info.width, height=staged_info.height,
        preset=style, apply_cuts=cut_pauses, audio_path=audio_path, cfg=job_cfg,
        track=load_track(work / "track.json"),
        behind=behind,
    )

    await say("Рисую субтитры", 0.80, "render")
    t = time.perf_counter()
    out = work / "result.mp4"
    if cfg.render_backend == "ass":
        await asyncio.to_thread(render_ass, plan, out, work_dir=work, cfg=job_cfg)
    else:
        async def on_render(done: float) -> None:
            await say("Рисую субтитры", render_share(done), "render")

        await arender_remotion(
            plan, out, assets_dir=work, job_key=work.name, cfg=job_cfg, on_progress=on_render,
        )
    mark("render", t)

    return JobResult(
        output=out,
        edl=edl,
        asr=ASRResult(words=list(edl.words), language="", backend="edl", model="—"),
        info=info,
        timings=timings,
        broll_used=len(edl.broll),
    )
