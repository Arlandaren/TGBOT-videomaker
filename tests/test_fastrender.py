"""Быстрый рендер без браузера: то же движение кадра, что в Remotion, и рабочий ролик."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from src.core.config import settings
from src.core.geometry import Rect as PlanRect
from src.core.render_plan import PlanBlock, PlanWord, RenderPlan
from src.pipeline import fastrender as fr
from src.pipeline.subs_ass import build_ass, font_em_scale

MOTION = Path(__file__).resolve().parents[1] / "render" / "src" / "visuals" / "motion.mjs"


def _js_const(name: str) -> float:
    m = re.search(rf"export const {name} = ([0-9.]+);", MOTION.read_text(encoding="utf-8"))
    assert m, name
    return float(m.group(1))


@pytest.mark.parametrize(
    "name",
    ["MIN_ANIM_FRAMES", "CUT_PUNCH", "CUT_SLIDE_RATIO", "CUT_SHAKE_RATIO",
     "CUT_SHAKE_TURNS", "CUT_FLASH", "CUT_COVER"],
)
def test_cut_constants_match_the_browser(name):
    assert getattr(fr, name) == _js_const(name)


def _plan(**meta) -> RenderPlan:
    return RenderPlan(version=1, fps=30, width=1080, height=1920, duration=4.0,
                      video="in.mp4", preset={}, meta=meta)


def test_no_camera_no_cut_means_the_frame_stays():
    assert fr.motion_at(_plan(), 10).still


def test_a_punch_starts_strong_and_settles():
    plan = _plan(cuts=[{"at": 1.0, "frames": 10, "kind": "punch"}])
    first = fr.motion_at(plan, 30)
    assert first.scale == pytest.approx(1 + fr.CUT_PUNCH)
    # Масштаб от центра кадра: центр остаётся на месте.
    assert first.scale * 540 + first.tx == pytest.approx(540)
    assert first.scale * 960 + first.ty == pytest.approx(960)
    assert fr.motion_at(plan, 39).scale < first.scale
    assert fr.motion_at(plan, 40).still


def test_the_camera_scales_around_its_point():
    plan = _plan(camera=[{"start": 0.0, "end": 2.0, "scale_from": 1.0, "scale_to": 1.2,
                          "ox": 300.0, "oy": 700.0}])
    m = fr.motion_at(plan, 30)  # середина плана
    assert m.scale == pytest.approx(1.1)
    assert m.scale * 300 + m.tx == pytest.approx(300)
    assert m.scale * 700 + m.ty == pytest.approx(700)


def test_flash_is_a_white_layer_not_a_move():
    m = fr.motion_at(_plan(cuts=[{"at": 0.0, "frames": 8, "kind": "flash"}]), 0)
    assert m.still and m.flash == pytest.approx(fr.CUT_FLASH)


def test_frames_round_like_javascript():
    # 0.5 кадра: Math.round даёт вверх, round в Python — к чётному.
    plan = _plan(cuts=[{"at": 2.5 / 30, "frames": 8, "kind": "punch"}])
    assert fr.motion_at(plan, 3).scale > 1
    assert fr.motion_at(plan, 2).still


def test_graphics_and_titles_go_to_the_browser_on_their_frames_only():
    plan = _plan(visuals={"overlays": [{"kind": "sticker", "start": 1.0, "end": 2.0, "out_frames": 9}]})
    plan.blocks = [_block(None), _block(None)]
    plan.blocks[1].title_card = True
    work = fr.browser_work(plan, 120)
    assert work.blocks == (1,)
    assert not work.under
    # Стикер — с запасом на появление и уход, заголовок 0..1 с — тоже.
    assert 28 in work.overlay and 60 + fr.TAIL_FRAMES in work.overlay
    assert 0 in work.overlay and 100 not in work.overlay


def test_broll_needs_whole_browser_frames():
    from src.core.render_plan import PlanBroll

    plan = _plan()
    plan.broll = [PlanBroll(start=1.0, end=1.5, anim="card", anchor="center", asset="a.jpg", query="")]
    work = fr.browser_work(plan, 120)
    assert 30 in work.under and 31 not in work.overlay
    assert fr.supports(plan)[0]
    plan.broll[0].end = 3.9  # почти весь ролик — быстрее обычным рендером
    assert not fr.supports(plan)[0]


def test_the_font_correction_matches_libass():
    """Montserrat: замерено, что libass рисует кегль 100 как браузер ~64."""
    scale = font_em_scale(settings.fonts_dir / "Montserrat-ExtraBold.ttf")
    assert scale == pytest.approx(1.562, abs=0.01)
    assert font_em_scale(None) == 1.0
    assert font_em_scale("нет-такого.ttf") == 1.0


def _block(rect: PlanRect | None) -> PlanBlock:
    word = PlanWord(text="Привет", start=0.0, end=1.0, emphasis="none", emoji=None,
                    word_idx=0, size=76.0)
    return PlanBlock(start=0.0, end=1.0, position="bottom", lines=[[word]], rect=rect)


def _ass_plan(rect: PlanRect | None) -> RenderPlan:
    from src.core.preset import load_preset

    return RenderPlan(version=1, fps=30, width=1080, height=1920, duration=1.0,
                      video="in.mp4", preset=load_preset("pulse").model_dump(),
                      blocks=[_block(rect)])


def test_by_default_the_ass_file_is_unchanged():
    ass = build_ass(_ass_plan(PlanRect(x=100, y=1300, width=400, height=200)))
    assert "\\pos(" not in ass and "\\an2" in ass


def test_the_fast_path_puts_the_block_into_its_rect_with_the_right_size():
    plan = _ass_plan(PlanRect(x=100, y=1300, width=400, height=200))
    ass = build_ass(plan, em_scale=1.5, place=True)
    assert "\\an5\\pos(300.0,1400.0)" in ass
    style = next(line for line in ass.splitlines() if line.startswith("Style:"))
    assert style.split(",")[2] == str(round(plan.preset["font"]["size"] * 1.5))
    # Без прямоугольника — прежнее выравнивание.
    assert "\\an2" in build_ass(_ass_plan(None), place=True)


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_type,nb_read_frames,width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return json.loads(out.stdout)


def test_a_whole_reel_renders_with_motion_bar_and_sound(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-nostdin",
         "-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=2",
         "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
        check=True, timeout=120,
    )
    plan = _ass_plan(PlanRect(x=40, y=400, width=280, height=80))
    plan.width, plan.height, plan.duration, plan.video = 360, 640, 2.0, str(src)
    plan.meta = {
        "cuts": [{"at": 0.5, "frames": 10, "kind": "punch"}],
        "progress": {"color": "#FF0000", "height": 8},
    }
    out = tmp_path / "out.mp4"
    seen: list[float] = []
    fr.render_fast(plan, out, work_dir=tmp_path, cfg=settings, on_progress=seen.append)

    meta = _probe(out)
    kinds = {s["codec_type"]: s for s in meta["streams"]}
    assert set(kinds) == {"video", "audio"}
    assert int(kinds["video"]["nb_read_frames"]) == 60
    assert (kinds["video"]["width"], kinds["video"]["height"]) == (360, 640)
    assert seen and seen[-1] == pytest.approx(1.0)

    # Полоса к концу ролика — почти во всю ширину и красная.
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-sseof", "-0.1", "-i", str(out),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, check=True, timeout=60,
    ).stdout
    top = np.frombuffer(raw, dtype=np.uint8).reshape(640, 360, 3)[2, 10:340]
    assert top[:, 0].mean() > 180 and top[:, 1].mean() < 90


# ---------------------------------------------------------------- выбор пути


def _cfg(mode: str):
    return settings.model_copy(update={"render_mode": mode})


@pytest.mark.anyio
async def test_a_failed_fast_build_falls_back_to_the_browser(tmp_path, monkeypatch):
    from src.pipeline import render as R

    def boom(*a, **kw):
        raise fr.FastRenderError("ffmpeg упал")

    monkeypatch.setattr(fr, "render_fast", boom)
    out = tmp_path / "r.mp4"
    out.write_bytes(b"half")  # огрызок сбойной сборки не должен остаться
    assert await R._arender_fast(_plan(), out, tmp_path, _cfg("fast"), 22, None) is None
    assert not out.exists()


@pytest.mark.anyio
async def test_hybrid_asks_the_browser_for_every_caption(tmp_path, monkeypatch):
    from src.pipeline import render as R

    seen = {}

    def fake(plan, out, **kw):
        seen.update(kw)
        kw["on_progress"](0.5)
        return out

    monkeypatch.setattr(fr, "render_fast", fake)
    got: list[float] = []

    async def progress(done):
        got.append(done)

    out = tmp_path / "r.mp4"
    assert await R._arender_fast(_plan(), out, tmp_path, _cfg("hybrid"), 22, progress) == out
    assert seen["all_captions"] is True and seen["crf"] == 22
    import asyncio

    await asyncio.sleep(0.05)
    assert got == [0.5]


def test_browser_is_the_default_mode():
    from src.core.config import Settings

    assert Settings.model_fields["render_mode"].default == "browser"


def test_the_fast_deadline_matches_the_render_deadline():
    from src.pipeline.render import RENDER_TIMEOUT

    assert fr.FAST_TIMEOUT == RENDER_TIMEOUT
