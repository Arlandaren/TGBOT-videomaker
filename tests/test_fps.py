"""Готовый ролик в output_fps: частота, куски дорисовки, запасные пути."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.core.config import Settings
from src.pipeline import fps as fps_mod
from src.pipeline.fps import chunks, target_fps, to_output_fps
from src.pipeline.probe import MediaInfo, probe


def cfg(**kw) -> Settings:
    return Settings(_env_file=None, bot_token="x", **kw)


def info_of(fps: float, duration: float = 4.0, has_audio: bool = True) -> MediaInfo:
    return MediaInfo(
        path="—", duration=duration, width=1080, height=1920, fps=fps, has_audio=has_audio,
        audio_channels=2, audio_rate=48000, rotation=0, video_codec="h264", bitrate=0,
    )


def frames_of(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(json.loads(out)["streams"][0]["nb_read_frames"])


@pytest.fixture(scope="module")
def clip30(tmp_path_factory) -> Path:
    """Три секунды 30 к/с с движущейся картинкой и звуком, маленький растр."""
    path = tmp_path_factory.mktemp("fps") / "clip30.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=144x256:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


# ------------------------------------------------------------------ частота


def test_the_target_is_the_setting_whatever_the_source():
    assert target_fps(29.97, cfg(output_fps=60)) == 60
    assert target_fps(120.0, cfg(output_fps=60)) == 60


def test_zero_keeps_the_source_rate():
    assert target_fps(29.97, cfg(output_fps=0)) == 30
    assert target_fps(0.0, cfg(output_fps=0, render_fps=25)) == 25


def test_the_source_rate_is_the_default():
    # 60 повтором кадров плавности не даёт, а рендер удваивает (см. config);
    # дорисовка по движению в лимиты воркера не помещается.
    assert cfg().output_fps == 0 and cfg().fps_interpolation == "repeat"
    assert target_fps(30.0, cfg()) == 30 and target_fps(59.94, cfg()) == 60


def test_a_reel_at_its_own_rate_skips_the_pass_by_default(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ffmpeg не должен запускаться")

    monkeypatch.setattr(fps_mod, "ffmpeg", boom)
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    assert to_output_fps(src, tmp_path / "s.mp4", info=info_of(30.0), cfg=cfg()) == src


# ------------------------------------------------------------------ бюджет


def fake_cgroup(tmp_path: Path, levels: dict[str, dict[str, str]]) -> tuple[Path, Path]:
    """Дерево cgroup v2 в папке: {"путь от корня": {"cpu.max": "...", ...}}."""
    root = tmp_path / "cgroup"
    deepest = ""
    for rel, files in levels.items():
        node = root / rel
        node.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (node / name).write_text(text + "\n")
        deepest = max(deepest, rel, key=len)
    proc = tmp_path / "proc-cgroup"
    proc.write_text(f"0::/{deepest}\n")
    return proc, root


def test_the_budget_is_the_quota_of_the_service_not_the_machine(tmp_path):
    proc, root = fake_cgroup(tmp_path, {
        "user/worker": {
            "cpu.max": "300000 100000",
            "memory.max": str(4 * 1024**3), "memory.current": str(1 * 1024**3),
        },
    })
    assert fps_mod.cgroup_budget(proc, root) == (3.0, 3 * 1024**3)


def test_the_tightest_limit_on_the_way_up_wins(tmp_path):
    proc, root = fake_cgroup(tmp_path, {
        "user": {"cpu.max": "200000 100000", "memory.max": "max", "memory.current": "5"},
        "user/worker": {
            "cpu.max": "max 100000",
            "memory.max": str(2 * 1024**3), "memory.current": str(1024**3),
        },
    })
    assert fps_mod.cgroup_budget(proc, root) == (2.0, 1024**3)


def test_no_cgroup_means_no_limit(tmp_path):
    assert fps_mod.cgroup_budget(tmp_path / "нет", tmp_path) == (None, None)


@pytest.mark.parametrize("cpus, memory_gb, expected", [
    (3.0, 3.0, 2),     # сервер: 3 ядра и 3 ГБ свободно — два куска по 0.9 ГБ
    (3.0, 16.0, 3),    # память есть — упираемся в квоту ядер
    (None, None, 6),   # ограничений нет — по числу ядер машины
    (0.5, 0.2, 1),     # меньше одного — всё равно один кусок
])
def test_chunks_fit_the_cores_and_memory_given_to_the_worker(monkeypatch, cpus, memory_gb, expected):
    monkeypatch.setattr(fps_mod.os, "cpu_count", lambda: 6)
    memory = None if memory_gb is None else int(memory_gb * 1024**3)
    monkeypatch.setattr(fps_mod, "cgroup_budget", lambda: (cpus, memory))
    assert fps_mod._workers(cfg()) == expected


def test_an_explicit_worker_count_is_taken_as_is(monkeypatch):
    monkeypatch.setattr(fps_mod, "cgroup_budget", lambda: (1.0, 1))
    assert fps_mod._workers(cfg(fps_workers=5)) == 5


# ------------------------------------------------------------------ куски


@pytest.mark.parametrize("total, workers", [(180, 6), (3121, 6), (7, 4), (600, 1), (1, 8)])
def test_chunks_cover_every_frame_once(total, workers):
    parts = chunks(total, 60, workers)
    assert sum(p.frames for p in parts) == total
    assert [p.first for p in parts] == [0, *[sum(q.frames for q in parts[:i + 1]) for i in range(len(parts) - 1)]]
    assert 1 <= len(parts) <= workers


def test_a_short_clip_is_not_cut_into_crumbs():
    # 3 с при 60 к/с: кусков не больше, чем помещается по две секунды.
    assert len(chunks(180, 60, 16)) == 1
    assert len(chunks(60 * 60, 60, 6)) == 6


# ------------------------------------------------------------------ проход


def test_a_file_already_at_the_rate_is_returned_untouched(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ffmpeg не должен запускаться")

    monkeypatch.setattr(fps_mod, "ffmpeg", boom)
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    out = to_output_fps(src, tmp_path / "s.mp4", info=info_of(60.0), cfg=cfg(output_fps=60))
    assert out == src


@pytest.mark.parametrize("mode, workers", [("motion", 1), ("motion", 3), ("repeat", 1)])
def test_thirty_becomes_sixty_with_sound_and_length_kept(clip30, tmp_path, monkeypatch, mode, workers):
    # Три куска по секунде: проверяются стыки склейки без перекодирования.
    monkeypatch.setattr(fps_mod, "MIN_CHUNK_SECONDS", 0.5)
    out = to_output_fps(
        clip30, tmp_path / f"{mode}.mp4", work_dir=tmp_path,
        cfg=cfg(output_fps=60, fps_interpolation=mode, fps_workers=workers),
    )
    assert out != clip30
    info = probe(out)
    assert info.fps == pytest.approx(60, abs=0.01)
    assert info.has_audio
    assert info.duration == pytest.approx(3.0, abs=0.1)
    assert frames_of(out) == pytest.approx(180, abs=2)
    assert not list(tmp_path.glob("fps-*")), "куски дорисовки остались на диске"


def test_motion_makes_new_frames_not_copies(clip30, tmp_path):
    """Дорисованный кадр отличается от обоих соседей; повтор — копия."""
    import numpy as np

    def grey(path: Path) -> np.ndarray:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "20",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, check=True,
        ).stdout
        return np.frombuffer(raw, np.uint8).reshape(-1, 256 * 144).astype(np.int16)

    motion = grey(to_output_fps(clip30, tmp_path / "m.mp4", work_dir=tmp_path,
                                cfg=cfg(output_fps=60, fps_interpolation="motion", fps_workers=1)))
    repeat = grey(to_output_fps(clip30, tmp_path / "r.mp4", work_dir=tmp_path,
                                cfg=cfg(output_fps=60, fps_interpolation="repeat")))

    def still(frames: np.ndarray) -> int:
        """Сколько соседних пар кадров почти одинаковы."""
        return int((np.abs(np.diff(frames, axis=0)).mean(axis=1) < 0.5).sum())

    assert still(repeat) >= 8
    assert still(motion) <= 1


def test_a_failed_interpolation_falls_back_to_repeat(clip30, tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("minterpolate упал")

    monkeypatch.setattr(fps_mod, "_motion", broken)
    out = to_output_fps(
        clip30, tmp_path / "s.mp4", work_dir=tmp_path,
        cfg=cfg(output_fps=60, fps_interpolation="motion"),
    )
    assert probe(out).fps == pytest.approx(60, abs=0.01)


def test_an_unreadable_file_leaves_the_reel_at_its_own_rate(tmp_path):
    src = tmp_path / "broken.mp4"
    src.write_bytes(b"not a video")
    out = to_output_fps(
        src, tmp_path / "s.mp4", info=info_of(30.0), work_dir=tmp_path, cfg=cfg(output_fps=60),
    )
    assert out == src
