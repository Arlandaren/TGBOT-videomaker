"""Несколько видео одного ролика → один исходник.

Человек снимает ролик кусками: дубль, ещё дубль, досъёмка. Приложение
принимает их по порядку, а монтаж умеет работать с одним файлом — поэтому
куски склеиваются в начале задачи, и дальше пайплайн не знает, что их было
несколько.

Склейка — с перекодированием. Куски с одного телефона обычно одинаковые, но
не обязаны: другая камера, другой fps, запись экрана без звука. `-c copy` на
таких стыках даёт рассинхрон или отказ, а узнаётся это уже в готовом ролике.

Кадр и частоту задаёт ПЕРВЫЙ кусок. Остальные вписываются в него целиком, с
полями: обрезка под чужой формат срезала бы говорящего, а поля уберёт
кадрирование 9:16, если лицо в кадре есть.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.core.proc import ffmpeg
from src.pipeline.probe import MediaInfo, probe

log = logging.getLogger(__name__)

# Сколько видео в одном ролике. Больше — это уже монтажный стол, а не «сняла
# три дубля подряд»; да и каждый кусок — отдельная загрузка с телефона.
MAX_CLIPS = 5
# Потолок частоты кадров склейки: 120 fps с телефона удвоили бы и время
# перекодирования, и размер исходника, а в ролике их всё равно не будет.
MAX_FPS = 60
AUDIO_RATE = 48000


def frame_of(first: MediaInfo) -> tuple[int, int, int]:
    """Кадр склейки: размеры первого куска (чётные — так требует x264) и его fps."""
    width = max(2, first.width - first.width % 2)
    height = max(2, first.height - first.height % 2)
    fps = min(MAX_FPS, int(round(first.fps)) or 30)
    return width, height, fps


def build_filter(infos: list[MediaInfo]) -> str:
    """filter_complex склейки. Выходы — `[v]` и `[a]`.

    Звук есть у склейки всегда: кусок без дорожки (запись экрана) получает
    тишину своей длины. Иначе `concat` отказал бы на первом же немом куске, а
    речь после него уехала бы относительно картинки.
    """
    width, height, fps = frame_of(infos[0])
    parts: list[str] = []
    for i, info in enumerate(infos):
        parts.append(
            f"[{i}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
            f"format=yuv420p,setpts=PTS-STARTPTS[v{i}]"
        )
        if info.has_audio:
            parts.append(
                f"[{i}:a:0]aresample={AUDIO_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            parts.append(
                f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=duration={max(info.duration, 0.1):.3f}[a{i}]"
            )
    order = "".join(f"[v{i}][a{i}]" for i in range(len(infos)))
    parts.append(f"{order}concat=n={len(infos)}:v=1:a=1[v][a]")
    return ";".join(parts)


def join_clips(clips: list[Path], dst: str | Path) -> Path:
    """Куски по порядку → один mp4. Один кусок — тоже можно, но незачем."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    infos = [probe(p) for p in clips]
    _, _, fps = frame_of(infos[0])
    args: list[str] = []
    for p in clips:
        args += ["-i", str(p)]
    args += [
        "-filter_complex", build_filter(infos), "-map", "[v]", "-map", "[a]",
        # Это ИСХОДНИК, его ещё дважды пережмут (нарезка, рендер): качество с
        # запасом, скорость важнее размера.
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart", str(dst),
    ]
    log.info("склейка: %d видео, %.1f с", len(clips), sum(i.duration for i in infos))
    ffmpeg(args, timeout=3600)
    return dst
