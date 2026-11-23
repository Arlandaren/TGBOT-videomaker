"""Живая графика: входы элемента, счётчик числа, полоса маркера, конфетти.

Формулы считает node тем же модулем, что уходит в кадр
(`render/src/visuals/motion.mjs`); выбор входа и счётчик — Python
(`src/core/overlays.py`). Сети и рендера нет.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.core.config import Settings
from src.core.edl import EDL, CaptionBlock, CaptionWord, Cut, Word
from src.core.overlay_models import MIN_ANIM_FRAMES, Overlay, Visuals
from src.core.overlays import count_of, dressed_plan
from src.core.render_plan import build_plan
from src.pipeline.visuals import VISUALS_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
MOTION = ROOT / "render" / "src" / "visuals" / "motion.mjs"
ENTRANCES = ("from_left", "from_right", "drop", "stamp", "wipe")

PROBE = """
import * as m from %s;
const fps = 30;
const base = { kind: "sticker", start: 1, end: 4, in_frames: 12, out_frames: 9,
  rect: { x: 100, y: 300, width: 200, height: 160 }, rotate: 0, seed: 3, float_px: 0 };
const out = { anims: {}, count: [], wipe: [] };
for (const anim of %s) {
  const rows = [];
  for (let f = 28; f <= 122; f++) rows.push({ f, ...m.overlayAt({ ...base, anim }, f, fps) });
  out.anims[anim] = rows;
}
const chip = { ...base, kind: "chip", anim: "wipe",
  count: { first: 0, last: 2, value: 9500000, frames: 21 } };
for (let f = 30; f <= 60; f++) {
  out.count.push(m.countAt(chip, f, fps));
  out.wipe.push(m.wipeAt(chip, f, fps));
}
out.plain = [m.countAt({ ...base }, 35, fps), m.wipeAt({ ...base, anim: "pop" }, 31, fps)];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def probe() -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("нет node — формулы движения не считаются")
    code = PROBE % (json.dumps(MOTION.as_uri()), json.dumps(list(ENTRANCES)))
    out = subprocess.run(
        [node, "--input-type=module", "-e", code], capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ------------------------------------------------------------------ формулы


@pytest.mark.parametrize("anim", ENTRANCES)
def test_an_entrance_is_invisible_outside_its_life_and_exact_at_rest(probe, anim):
    rows = {r["f"]: r for r in probe["anims"][anim]}
    # Кадры `start` (30) и `end` (120) и всё вне их — не видны.
    for f in (28, 29, 30, 120, 121, 122):
        assert not rows[f]["visible"] and rows[f]["opacity"] == 0, (anim, f)
    # Покой: между концом входа (кадр 42) и началом выхода (кадр 111).
    for f in range(43, 111):
        row = rows[f]
        assert row["opacity"] == 1 and row["scale"] == 1, (anim, f, row)
        assert row["dx"] == 0 and row["dy"] == 0 and row["rotate"] == 0, (anim, f, row)


@pytest.mark.parametrize("anim", ENTRANCES)
def test_an_entrance_fades_in_without_blinking(probe, anim):
    rows = probe["anims"][anim]
    enter = [r["opacity"] for r in rows if 31 <= r["f"] <= 42]
    leave = [r["opacity"] for r in rows if 111 <= r["f"] <= 119]
    assert enter == sorted(enter) and leave == sorted(leave, reverse=True), anim
    assert 0 < enter[0] and max(b - a for a, b in zip([0, *enter], enter)) <= 0.9, anim


def test_each_entrance_moves_the_way_it_is_named(probe):
    first = {anim: probe["anims"][anim][3] for anim in ENTRANCES}  # кадр 31
    assert first["from_left"]["dx"] < -40 and first["from_right"]["dx"] > 40
    assert first["drop"]["dy"] < -60 and first["drop"]["dx"] == 0
    assert first["stamp"]["scale"] > 1.3
    assert (first["wipe"]["scale"], first["wipe"]["dx"], first["wipe"]["dy"]) == (1, 0, 0)
    # Штамп садится без перелёта: масштаб на входе только убывает.
    stamp = [r["scale"] for r in probe["anims"]["stamp"] if 31 <= r["f"] <= 43]
    assert stamp == sorted(stamp, reverse=True) and min(stamp) == 1
    # Падение: один отскок вверх после касания, не больше десятой хода.
    drop = [r["dy"] for r in probe["anims"]["drop"] if 31 <= r["f"] <= 43]
    assert min(drop) < -100 and max(drop) <= 0
    assert max(abs(v) for v in drop[9:]) < 0.1 * abs(min(drop))


def test_the_counter_runs_up_to_the_number_and_keeps_its_digits(probe):
    seen = [v for v in probe["count"] if v is not None]
    assert len(seen) == 21, "счёт идёт ровно свои кадры и потом отдаёт слово как есть"
    assert seen == sorted(seen) and seen[0] >= 1_000_000 and seen[-1] <= 9_500_000
    assert all(len(str(v)) == 7 for v in seen), "число цифр на счёте не меняется"
    assert seen[-1] > 9_000_000 and probe["count"][21] is None
    assert probe["plain"][0] is None


def test_the_marker_bar_is_drawn_left_to_right_once(probe):
    wipe = probe["wipe"]
    assert wipe[0] == 0 and wipe == sorted(wipe) and wipe[12] == 1 and wipe[-1] == 1
    assert probe["plain"][1] == 1, "у элемента с другим входом полоса проведена сразу"


# ------------------------------------------------------------------ выбор Python


@pytest.mark.parametrize(
    "words,expected",
    [
        (["9", "500", "000", "₽"], (0, 2, 9_500_000)),
        (["+21%"], (0, 0, 21)),
        (["за", "30", "дней"], (1, 1, 30)),
        (["$1", "200"], (0, 1, 1200)),
        (["11,5", "млн"], None),          # дробь не считается
        (["с", "5", "до", "10"], None),   # два числа
        (["×3"], None),                   # меньше десяти — считать нечего
        (["одна", "истина"], None),
    ],
)
def test_count_of_finds_the_single_whole_number(words, expected):
    got = count_of(words, 30)
    if expected is None:
        assert got is None
    else:
        assert (got["first"], got["last"], got["value"]) == expected
        assert got["frames"] >= MIN_ANIM_FRAMES


def talk(n: int = 60) -> EDL:
    words = [
        Word(idx=i, text=f"слово{i}", start=round(i * 0.5, 3), end=round(i * 0.5 + 0.4, 3))
        for i in range(n)
    ]
    captions = [
        CaptionBlock(words=[CaptionWord(word_idx=j) for j in range(i, min(i + 3, n))])
        for i in range(0, n, 3)
    ]
    return EDL(
        source_duration=n * 0.5, words=words,
        cuts=[Cut(src_in=0.0, src_out=n * 0.5, reason="keep")], captions=captions, preset="pulse",
    )


def made(sheet: Visuals) -> list[dict[str, Any]]:
    plan = dressed_plan(
        build_plan, talk(), "video.mp4", visuals=sheet, apply_cuts=False,
        cfg=Settings(_env_file=None, bot_token="x"),
    )
    return plan.meta["visuals"]["overlays"]


def test_the_entrance_follows_the_kind_and_the_place():
    got = made(
        Visuals(
            overlays=[
                Overlay(kind="chip", word_idx=4, text="9 500 000 ₽", style="tag"),
                Overlay(kind="chip", word_idx=12, text="считайте сами", style="marker"),
                Overlay(kind="sticker", word_idx=20, emoji="🔥", slot="top_left"),
                Overlay(kind="sticker", word_idx=28, emoji="🚀", slot="top_right"),
                Overlay(kind="decor", word_idx=36, glyph="confetti"),
            ]
        )
    )
    by_kind = {(o["kind"], o["style"] or o["glyph"] or o["emoji"]): o for o in got}
    tag = by_kind[("chip", "tag")]
    assert tag["anim"] == "stamp" and tag["count"]["value"] == 9_500_000
    marker = by_kind[("chip", "marker")]
    assert marker["anim"] == "wipe" and marker["count"] is None and marker["rotate"] != 0
    assert by_kind[("sticker", "🔥")]["anim"] == "from_left"
    assert by_kind[("sticker", "🚀")]["anim"] == "from_right"
    confetti = by_kind[("decor", "confetti")]
    assert len(confetti["marks"]) >= 8, "конфетти — много мелких кусочков"


def test_the_model_may_ask_for_a_marker_and_confetti():
    overlay = VISUALS_SCHEMA["properties"]["overlays"]["items"]["properties"]
    assert "marker" in overlay["style"]["enum"] and "confetti" in overlay["glyph"]["enum"]
