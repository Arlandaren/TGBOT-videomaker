"""Камера рендера: планы, масштаб по кадрам, трек лица после камеры."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.camera import (
    DEFAULT_ORIGIN,
    PlanShot,
    filmed_plan,
    plan_camera,
    state_at,
    zoom_track,
)
from src.core.edl import EDL, CaptionBlock, CaptionWord, Cut, Word
from src.core.preset import load_preset
from src.core.track import CropWindow, FaceTrack, TrackPoint

RENDER = Path(__file__).resolve().parents[1] / "render"
FRAME = (1080, 1920)
FPS = 30


def edl_of(cuts: list[tuple[float, float, str, str]], duration: float, **extra) -> EDL:
    words = [Word(idx=0, text="раз", start=0.1, end=0.4)]
    return EDL(
        source_duration=duration,
        words=words,
        cuts=[Cut(src_in=a, src_out=b, reason=r, framing=f) for a, b, r, f in cuts],
        captions=[CaptionBlock(words=[CaptionWord(word_idx=0)])],
        preset="pulse",
        **extra,
    )


def track_of(seconds: float, *, cx=0.5, cy=0.4, h=0.25, zooms=None) -> FaceTrack:
    return FaceTrack(
        fps=FPS,
        points=[TrackPoint(f=i, cx=cx, cy=cy, h=h, conf=0.9) for i in range(round(seconds * FPS))],
        windows=[CropWindow(start=a, end=b, zoom=z) for a, b, z in (zooms or [])],
    )


TWO_SHOTS = [(0.0, 5.0, "keep", "wide"), (5.0, 6.0, "drop_silence", "wide"), (6.0, 11.0, "keep", "tight")]


def shots_of(edl, **k):
    return plan_camera(edl, preset=load_preset("pulse"), frame=FRAME, fps=FPS, **k)


def test_a_shot_pushes_in_slowly_and_never_past_the_cap():
    cam = load_preset("pulse").camera
    (short,) = shots_of(edl_of([(0.0, 2.0, "keep", "wide")], 2.0))
    assert short.scale_from == 1.0
    assert short.scale_to == pytest.approx(1.0 + cam.drift_per_s * 2.0)
    (long,) = shots_of(edl_of([(0.0, 60.0, "keep", "wide")], 60.0))
    assert long.scale_to == pytest.approx(1.0 + cam.drift_max)


def test_scale_jumps_only_on_the_cut():
    """Внутри плана масштаб растёт ровно; скачок — только на границе планов."""
    edl = edl_of(TWO_SHOTS, 11.0)
    shots = shots_of(edl, track=track_of(10.0, zooms=[(0.0, 10.0, 1.0)]))
    assert [(s.start, s.end) for s in shots] == [(0.0, 5.0), (5.0, 10.0)]
    scales = [state_at(shots, f, FPS)[0] for f in range(300)]
    steps = [b - a for a, b in zip(scales, scales[1:])]
    jumps = [i + 1 for i, d in enumerate(steps) if abs(d) > 0.002]
    assert jumps == [150]
    assert all(d > 0 for i, d in enumerate(steps) if i + 1 != 150)


def test_the_render_adds_the_punch_the_crop_could_not_give():
    cam = load_preset("pulse").camera
    edl = edl_of(TWO_SHOTS, 11.0)
    # Вертикальное 1080p: окно кропа одно на весь ролик.
    flat = shots_of(edl, track=track_of(10.0, zooms=[(0.0, 10.0, 1.0)]))
    assert flat[1].scale_from == pytest.approx(cam.punch_scale)
    # Кропу было куда сужаться — второй раз не укрупняем.
    done = shots_of(edl, track=track_of(10.0, zooms=[(0.0, 5.0, 1.0), (5.0, 10.0, 1.27)]))
    assert done[1].scale_from == 1.0
    # Что сделал кроп, неизвестно — тоже не укрупняем.
    assert shots_of(edl, track=track_of(10.0))[1].scale_from == 1.0
    assert shots_of(edl)[1].scale_from == 1.0


def test_no_camera_when_it_is_switched_off_or_the_frame_is_split():
    cuts = [(0.0, 8.0, "keep", "wide")]
    assert shots_of(edl_of(cuts, 8.0, camera=False)) == []
    assert shots_of(edl_of(cuts, 8.0, split_until=3.0)) == []
    off = load_preset("pulse").model_copy(deep=True)
    off.camera.enabled = False
    assert plan_camera(edl_of(cuts, 8.0), preset=off, frame=FRAME, fps=FPS) == []


def test_a_sheet_saved_before_the_field_keeps_the_camera():
    raw = edl_of([(0.0, 8.0, "keep", "wide")], 8.0).model_dump()
    raw.pop("camera")
    assert EDL(**raw).camera is True


def test_without_cuts_the_whole_reel_is_one_shot():
    edl = edl_of(TWO_SHOTS, 11.0)
    (shot,) = shots_of(edl, apply_cuts=False)
    assert (shot.start, shot.end, shot.scale_from) == (0.0, 11.0, 1.0)


def test_shot_borders_follow_the_real_file_like_the_crop_does():
    """Файл на пару кадров длиннее листа — границы растягиваются вместе с ним."""
    edl = edl_of(TWO_SHOTS, 11.0)
    shots = shots_of(edl, video_duration=10.08)
    assert shots[0].end == shots[1].start == pytest.approx(round(5.04 * FPS) / FPS, abs=1e-3)


def test_the_zoom_goes_into_the_forehead_and_stays_inside_the_frame():
    edl = edl_of([(0.0, 6.0, "keep", "wide")], 6.0)
    (shot,) = shots_of(edl, track=track_of(6.0, cx=0.4, cy=0.4, h=0.2))
    assert (shot.ox, shot.oy) == (pytest.approx(0.4 * 1080), pytest.approx(0.3 * 1920))
    (edge,) = shots_of(edl, track=track_of(6.0, cx=0.02, cy=0.05, h=0.2))
    assert edge.ox == pytest.approx(0.25 * 1080) and edge.oy == pytest.approx(0.12 * 1920)
    (blind,) = shots_of(edl)
    assert (blind.ox / 1080, blind.oy / 1920) == pytest.approx(DEFAULT_ORIGIN)


def test_state_holds_the_last_shot_and_rests_before_the_first():
    shots = [PlanShot(1.0, 2.0, 1.0, 1.02, 540, 800), PlanShot(3.0, 4.0, 1.1, 1.12, 540, 800)]
    assert state_at(shots, 0, FPS)[0] == 1.0
    assert state_at(shots, 75, FPS)[0] == 1.0  # между планами
    assert state_at(shots, 90, FPS)[0] == pytest.approx(1.1)
    assert state_at(shots, 500, FPS)[0] == pytest.approx(1.12)
    assert state_at([], 10, FPS) == (1.0, 0.0, 0.0)


def test_the_track_is_seen_where_the_face_lands_after_the_camera():
    shots = [PlanShot(0.0, 1.0, 1.1, 1.1, 540.0, 576.0)]
    seen = zoom_track(track_of(1.0, cx=0.5, cy=0.4, h=0.25), shots, FRAME)
    p = seen.points[10]
    assert p.cx == pytest.approx(0.5)
    assert p.cy == pytest.approx((576 + 1.1 * (0.4 * 1920 - 576)) / 1920)
    assert p.h == pytest.approx(0.275)
    still = track_of(1.0)
    assert zoom_track(still, [], FRAME) is still


def test_the_plan_carries_the_camera_and_the_fast_path_does_not():
    edl = edl_of([(0.0, 6.0, "keep", "wide")], 6.0)
    args = dict(fps=FPS, width=1080, height=1920, preset=load_preset("pulse"), track=track_of(6.0))
    plan = filmed_plan(edl, "v.mp4", backend="remotion", **args)
    (shot,) = plan.to_dict()["meta"]["camera"]
    assert shot["scale_to"] > shot["scale_from"] == 1.0
    assert "camera" not in filmed_plan(edl, "v.mp4", backend="ass", **args).meta


def test_the_renderer_mirrors_the_frame_arithmetic():
    """`Camera.ts` считает кадры так же, как `state_at`: одна формула на двоих."""
    src = (RENDER / "src" / "layers" / "Camera.ts").read_text(encoding="utf-8")
    assert "Math.round(shot.start * fps)" in src and "Math.round(shot.end * fps)" in src
    assert "Math.max(1, stop - first)" in src
    assert not re.search(r"Math\.random|spring\(|Math\.sin", src)
    types = (RENDER / "src" / "types.ts").read_text(encoding="utf-8")
    block = types[types.index("export type PlanShot") :]
    for name in PlanShot.__dataclass_fields__:
        assert f"{name}: number" in block[: block.index("};")]
