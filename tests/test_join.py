"""Ролик из нескольких видео: склейка кусков в один исходник."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.pipeline import join
from src.pipeline.probe import MediaInfo, probe

pytestmark = pytest.mark.anyio


def _info(*, width=1080, height=1920, fps=30.0, has_audio=True, duration=5.0) -> MediaInfo:
    return MediaInfo(
        path="x.mp4", duration=duration, width=width, height=height, fps=fps, has_audio=has_audio,
        audio_channels=2 if has_audio else 0, audio_rate=48000 if has_audio else 0,
        rotation=0, video_codec="h264", bitrate=0,
    )


def test_the_first_clip_sets_the_frame_for_all_of_them():
    """Кадр и частоту задаёт первый кусок: остальные вписываются в него, а не
    наоборот. Нечётные размеры x264 не принимает, 120 fps склейке не нужны."""
    assert join.frame_of(_info(width=1081, height=1919, fps=119.88)) == (1080, 1918, 60)

    fc = join.build_filter([_info(), _info(width=1920, height=1080, fps=25.0)])
    assert fc.count("scale=1080:1920:force_original_aspect_ratio=decrease") == 2
    assert fc.count("fps=30") == 2
    assert fc.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")


def test_a_silent_clip_gets_silence_of_its_own_length():
    """Кусок без звука (запись экрана) не должен ронять склейку и сдвигать речь
    следующих кусков: ему подставляется тишина ровно его длины."""
    fc = join.build_filter([_info(), _info(has_audio=False, duration=7.25), _info()])
    assert "[1:a:0]" not in fc
    assert "anullsrc=r=48000:cl=stereo,atrim=duration=7.250[a1]" in fc
    assert "[0:a:0]" in fc and "[2:a:0]" in fc


def _make(path: Path, *, size: str, seconds: float, audio: bool) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s={size}:r=25:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}"]
    cmd += ["-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def test_clips_of_different_shapes_join_into_one_playable_video(tmp_path):
    """Настоящий ffmpeg: вертикальный кусок со звуком и горизонтальный без —
    на выходе один файл в кадре первого, длиной в сумму, со звуковой дорожкой."""
    a = _make(tmp_path / "a.mp4", size="180x320", seconds=2, audio=True)
    b = _make(tmp_path / "b.mp4", size="320x180", seconds=1.5, audio=False)

    out = join.join_clips([a, b], tmp_path / "out.mp4")

    info = probe(out)
    assert (info.width, info.height) == (180, 320)
    assert info.has_audio
    assert info.duration == pytest.approx(3.5, abs=0.25)


async def test_the_worker_joins_parts_once_and_leaves_one_source(tmp_path, monkeypatch):
    """Склейка ложится на место исходника, счётчик кусков обнуляется, куски
    удаляются: перерендер и повтор задачи видят обычный ролик из одного файла."""
    from src.worker import tasks

    job = SimpleNamespace(id="job-1", source_key="sources/job-1/source.mp4", source_parts=2)
    src = tmp_path / "source.mp4"
    src.write_bytes(b"first")
    calls: dict[str, list] = {"download": [], "upload": [], "delete": [], "update": [], "join": []}

    async def download(key, dst):
        calls["download"].append(key)
        Path(dst).write_bytes(key.encode())

    async def upload(path, key, **_):
        calls["upload"].append((Path(path).read_bytes(), key))

    async def delete(key):
        calls["delete"].append(key)

    async def update_job(job_id, **values):
        calls["update"].append(values)

    def fake_join(clips, dst):
        calls["join"].append([Path(c).name for c in clips])
        Path(dst).write_bytes(b"joined")
        return Path(dst)

    monkeypatch.setattr(tasks.storage, "download", download)
    monkeypatch.setattr(tasks.storage, "upload", upload)
    monkeypatch.setattr(tasks.storage, "delete", delete)
    monkeypatch.setattr(tasks.repo, "update_job", update_job)
    monkeypatch.setattr(join, "join_clips", fake_join)

    got = await tasks.join_source_parts(job, src, tmp_path)

    assert got == src and src.read_bytes() == b"joined"
    assert calls["join"] == [["source.mp4", "part1.mp4", "part2.mp4"]], "порядок кусков — порядок выбора"
    assert calls["upload"] == [(b"joined", "sources/job-1/source.mp4")]
    assert calls["update"] == [{"source_parts": 0}]
    assert calls["delete"] == [tasks.part_key("job-1", 1), tasks.part_key("job-1", 2)]
    assert not list(tmp_path.glob("part*.mp4")), "куски не должны оставаться в рабочей папке"

    # Обычный ролик: ни скачиваний, ни склейки.
    plain = SimpleNamespace(id="job-2", source_key="k", source_parts=None)
    assert await tasks.join_source_parts(plain, src, tmp_path) == src
    assert len(calls["join"]) == 1


async def test_a_lost_part_is_a_plain_refusal(tmp_path, monkeypatch):
    """Кусок пропал из хранилища — понятный отказ с возвратом, а не ролик из
    половины видео."""
    from src.pipeline.job import JobError
    from src.worker import tasks

    async def download(key, dst):
        raise FileNotFoundError(key)

    monkeypatch.setattr(tasks.storage, "download", download)
    job = SimpleNamespace(id="job-3", source_key="k", source_parts=1)
    with pytest.raises(JobError) as e:
        await tasks.join_source_parts(job, tmp_path / "source.mp4", tmp_path)
    assert e.value.code == "source_lost"
