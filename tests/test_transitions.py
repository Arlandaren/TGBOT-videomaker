"""Переходы: лист → секунды и кадры (`src/core/transitions.py`) и формула рендера.

Формула `cutAt` живёт в `render/src/visuals/motion.mjs` — её считает node, тем
же модулем, что уходит в кадр. Сети нет, рендера нет.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.core.camera import filmed_plan
from src.core.config import Settings
from src.core.edl import EDL, CaptionBlock, CaptionWord, Cut, Word
from src.core.overlay_models import (
    MIN_ANIM_FRAMES,
    SHOUT_PUNCH_MAX,
    TRANSITION_HEAD,
    TRANSITION_MIN_GAP,
    Scene,
    Transition,
    Visuals,
)
from src.core.overlay_rules import sanitize_visuals
from src.core.preset import load_preset
from src.core.transitions import cut_times
from src.pipeline.audio_post import SFX_SWISH, plan_sfx
from src.pipeline.visuals import cut_words, fallback_transitions, fallback_visuals

ROOT = Path(__file__).resolve().parents[1]
MOTION = ROOT / "render" / "src" / "visuals" / "motion.mjs"
FPS = 30
STEP = 0.5


def talk(n: int = 60, *, cuts: list[Cut] | None = None, shout: tuple[int, ...] = ()) -> EDL:
    """Ровная речь: слово `i` звучит с `i * STEP`, блоки по три слова."""
    words = [
        Word(idx=i, text=f"слово{i}", start=round(i * STEP, 3), end=round(i * STEP + 0.4, 3))
        for i in range(n)
    ]
    captions = [
        CaptionBlock(
            words=[
                CaptionWord(word_idx=j, emphasis="shout" if j in shout else "none")
                for j in range(i, min(i + 3, n))
            ]
        )
        for i in range(0, n, 3)
    ]
    total = n * STEP
    return EDL(
        source_duration=total,
        words=words,
        cuts=cuts or [Cut(src_in=0.0, src_out=total, reason="keep")],
        captions=captions,
        preset="pulse",
    )


def three_pieces(n: int = 60) -> list[Cut]:
    """Три оставленных куска и две вырезанные паузы между ними."""
    return [
        Cut(src_in=0.0, src_out=9.0, reason="hook"),
        Cut(src_in=9.0, src_out=10.0, reason="drop_filler"),
        Cut(src_in=10.0, src_out=20.0, reason="keep", framing="tight"),
        Cut(src_in=20.0, src_out=21.0, reason="drop_filler"),
        Cut(src_in=21.0, src_out=n * STEP, reason="keep", framing="tight"),
    ]


def plan_of(edl: EDL, **cfg: Any):
    duration = edl.timeline().duration
    return filmed_plan(
        edl, "video.mp4", backend="remotion", video_duration=duration, fps=FPS,
        width=1080, height=1920, preset=load_preset("pulse"), apply_cuts=True,
        cfg=Settings(_env_file=None, bot_token="x", **cfg),
    )


def cuts_of(edl: EDL, **cfg: Any) -> list[dict[str, Any]]:
    return plan_of(edl, **cfg).meta.get("cuts", [])


# ------------------------------------------------------------------ лист


def test_cut_words_name_the_first_word_after_each_cut_and_the_reframing():
    edl = talk(cuts=three_pieces())
    assert cut_words(edl) == [(20, True), (42, False)]
    assert cut_times(edl, apply_cuts=True, duration=edl.timeline().duration) == [9.0, 19.0]
    assert cut_words(talk()) == []


def test_the_free_planner_puts_a_transition_on_every_cut():
    edl = talk(cuts=three_pieces())
    got = {t.word_idx: t.kind for t in fallback_transitions(edl)}
    assert got[20] == "punch", "смена крупности — наезд"
    assert got[42] in ("slide_left", "slide_right", "flash")
    assert fallback_visuals(edl).transitions, "переходы доезжают до листа"


def test_sanitize_keeps_transitions_and_drops_the_broken_ones():
    edl = talk(cuts=three_pieces())
    raw = {
        "overlays": [],
        "scenes": [],
        "transitions": [
            {"word_idx": 42, "kind": "flash"},
            {"word_idx": 20, "kind": "punch"},
            {"word_idx": 20, "kind": "shake"},      # второй на то же слово
            {"word_idx": 19, "kind": "punch"},      # слово вырезано
            {"word_idx": 999, "kind": "punch"},     # слова нет
            {"word_idx": 30, "kind": "teleport"},   # вида нет
            "мусор",
        ],
    }
    clean = sanitize_visuals(raw, edl)
    assert [(t.word_idx, t.kind) for t in clean.transitions] == [(20, "punch"), (42, "flash")]
    assert sanitize_visuals(clean, edl) == clean, "повторная чистка ничего не меняет"
    assert EDL.model_validate_json(
        edl.model_copy(update={"visuals": clean}).model_dump_json()
    ).visuals == clean


def test_a_scene_that_runs_too_long_is_trimmed_not_dropped():
    """Модель просит сцену на 10 с — остаются первые шесть, а не ничего."""
    edl = talk()
    long = Visuals(scenes=[Scene(layout="phone", word_from=10, word_to=30)])
    clean = sanitize_visuals(long, edl)
    assert len(clean.scenes) == 1
    kept = clean.scenes[0]
    assert kept.word_from == 10 and kept.word_to < 30
    length = edl.words[kept.word_to].end - edl.words[10].start
    assert 4.5 <= length <= 6.0 + 1e-6


# ------------------------------------------------------------------ план


def test_transitions_land_on_the_cut_and_on_the_frame_grid():
    edl = talk(cuts=three_pieces())
    edl.visuals = Visuals(
        transitions=[Transition(word_idx=20, kind="punch"), Transition(word_idx=42, kind="flash")]
    )
    cuts = cuts_of(edl)
    assert [(c["at"], c["kind"]) for c in cuts] == [(9.0, "punch"), (19.0, "flash")]
    for cut in cuts:
        assert cut["frames"] >= MIN_ANIM_FRAMES
        assert abs(cut["at"] * FPS - round(cut["at"] * FPS)) < 1e-6


def test_transitions_keep_their_distance_and_stay_off_the_edges():
    edl = talk()
    edl.visuals = Visuals(
        transitions=[Transition(word_idx=i, kind="punch") for i in range(0, 60)]
    )
    cuts = cuts_of(edl)
    times = [c["at"] for c in cuts]
    assert len(times) >= 8
    assert all(b - a >= TRANSITION_MIN_GAP - 1e-6 for a, b in zip(times, times[1:]))
    assert times[0] >= TRANSITION_HEAD and times[-1] < edl.timeline().duration - 0.6


def test_shout_words_get_a_punch_but_only_a_few():
    edl = talk(90, shout=tuple(range(4, 90, 3)))
    cuts = cuts_of(edl)
    assert 1 <= len(cuts) <= SHOUT_PUNCH_MAX
    assert {c["kind"] for c in cuts} == {"punch"}


def test_no_transition_while_a_scene_moves_in_or_out():
    edl = talk()
    edl.visuals = Visuals(
        scenes=[Scene(layout="phone", word_from=10, word_to=20)],
        transitions=[Transition(word_idx=i, kind="flash") for i in (10, 11, 16, 20, 21, 30)],
    )
    plan = plan_of(edl)
    scene = plan.meta["visuals"]["scenes"][0]
    for cut in plan.meta["cuts"]:
        enters = scene["start"] <= cut["at"] <= scene["start"] + scene["in_frames"] / FPS
        leaves = scene["end"] - scene["out_frames"] / FPS <= cut["at"] <= scene["end"]
        assert not enters and not leaves, cut
    assert any(c["at"] == 15.0 for c in plan.meta["cuts"]), "переход вне сцены остался"


def test_the_switches_turn_transitions_and_the_bar_off():
    edl = talk(cuts=three_pieces())
    edl.visuals = Visuals(transitions=[Transition(word_idx=20, kind="punch")])
    on = plan_of(edl).meta
    assert on["cuts"] and on["progress"]["height"] >= 4 and on["progress"]["color"].startswith("#")
    off = plan_of(edl, transitions_enabled=False, progress_bar=False).meta
    assert "cuts" not in off and "progress" not in off
    # У переходов своя галочка: «Движение камеры» их больше не выключает.
    still = edl.model_copy(update={"camera": False})
    assert plan_of(still).meta["cuts"]
    assert "cuts" not in plan_of(still, transitions_enabled=False).meta


def test_a_slide_gets_a_swish_and_the_others_stay_silent():
    edl = talk()
    edl.visuals = Visuals(
        transitions=[
            Transition(word_idx=10, kind="slide_left"),
            Transition(word_idx=20, kind="punch"),
            Transition(word_idx=30, kind="flash"),
            Transition(word_idx=40, kind="slide_right"),
        ]
    )
    got = [(s.at_word_idx, s.name) for s in plan_sfx(edl, preset=load_preset("pulse"))]
    assert got == [(10, SFX_SWISH), (40, SFX_SWISH)]


# ------------------------------------------------------------------ формула рендера


PROBE = """
import * as m from %s;
const [W, H, fps] = [1080, 1920, 30];
const cuts = ["punch", "slide_left", "slide_right", "shake", "flash"].map(
  (kind, i) => ({ at: 1 + i * 2, frames: 10, kind }),
);
const rows = [];
for (let f = 0; f < 12 * fps; f++) rows.push({ f, ...m.cutAt(cuts, f, fps, W, H) });
console.log(JSON.stringify({ rows, short: m.cutAt([{ at: 1, frames: 2, kind: "punch" }], 36, fps, W, H),
  junk: m.cutAt("мусор", 30, fps, W, H), same: m.cutAt(cuts, 31, fps, W, H) }));
"""


@pytest.fixture(scope="module")
def probe() -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("нет node — формула перехода не считается")
    code = PROBE % json.dumps(MOTION.as_uri())
    out = subprocess.run(
        [node, "--input-type=module", "-e", code], capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_video_rests_outside_a_transition(probe):
    busy = {f for start in (30, 90, 150, 210, 270) for f in range(start, start + 10)}
    for row in probe["rows"]:
        if row["f"] not in busy:
            assert (row["scale"], row["dx"], row["dy"], row["flash"]) == (1, 0, 0, 0), row
    assert probe["junk"] == {"scale": 1, "dx": 0, "dy": 0, "flash": 0}


def test_a_transition_never_shows_the_edge_of_the_video(probe):
    """Сдвиг покрыт масштабом: кадр ни разу не открывает край видео."""
    moved = [r for r in probe["rows"] if r["dx"] or r["dy"]]
    assert moved, "ни один переход не двигает кадр — тест пуст"
    for row in moved:
        assert (row["scale"] - 1) * 1080 / 2 > abs(row["dx"]), row
        assert (row["scale"] - 1) * 1920 / 2 > abs(row["dy"]), row


def test_each_kind_starts_strong_and_decays(probe):
    rows = {r["f"]: r for r in probe["rows"]}
    punch = [rows[f]["scale"] for f in range(30, 40)]
    assert punch[0] > 1.08 and punch == sorted(punch, reverse=True) and punch[-1] < 1.01
    assert rows[90]["dx"] > 40 and rows[150]["dx"] < -40, "въезды идут с разных сторон"
    flash = [rows[f]["flash"] for f in range(270, 280)]
    assert 0.5 < flash[0] <= 1 and flash == sorted(flash, reverse=True)
    shake = [rows[f]["dx"] for f in range(210, 220)]
    assert max(shake) > 2 and min(shake) < -2, "тряска идёт в обе стороны"
    assert all(rows[f]["scale"] == 1 for f in range(270, 280)), "вспышка не двигает кадр"


def test_a_transition_is_never_shorter_than_eight_frames(probe):
    """Число плана зажато порогом: два кадра — это мигание."""
    assert probe["short"]["scale"] > 1, "на шестом кадре короткий переход уже кончился"


def test_the_formula_is_deterministic(probe):
    assert probe["same"] == {k: v for k, v in probe["rows"][31].items() if k != "f"}
    source = MOTION.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("*", "/")))
    assert "Math.random" not in code and "Date" not in code
