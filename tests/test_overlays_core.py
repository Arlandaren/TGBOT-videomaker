"""Графика поверх ролика: лист + план → `plan.meta["visuals"]` (`src/core/overlays.py`).

Что здесь сторожится. Графика — гость в кадре: она уступает субтитру, вставке и
лицу и никогда их не двигает; лишнее молча выпадает, ролик от этого не падает;
всё время стоит на сетке кадров; а перерендер того же листа даёт тот же план
байт в байт. Каждое из этих правил проверяется на всех темах сразу — тема
приносит свой шрифт, кегль и место субтитра, и раскладка обязана пережить любую.

Числа раскладки (где именно стоит плашка) здесь не закрепляются: это дизайн, и
он будет меняться. Закрепляются свойства, без которых кадр сломан.
"""

from __future__ import annotations

import copy
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from src.core import overlays
from src.core.config import Settings
from src.core.edl import EDL, AnchorTrack, Broll, CaptionBlock, CaptionWord, Cut, Word
from src.core.geometry import Rect, line_box_height, measure_line, metrics_for
from src.core.overlay_models import (
    MIN_ANIM_FRAMES,
    Overlay,
    PlanScene,
    PlanSceneBg,
    Scene,
    SceneItem,
    Visuals,
)
from src.core.overlays import (
    chip_text,
    dressed_plan,
    face_at,
    hit_box,
    luminance,
    mark_box,
    measure_chip,
    sfx_events,
    stage_at,
    stage_visual_assets,
)
from src.core.preset import available, load_preset
from src.core.render_plan import (
    FACE_GUARD_DOWN,
    FACE_GUARD_UP,
    FACE_MIN_CONF,
    FACE_WIDTH_RATIO,
    RenderPlan,
    build_plan,
)
from src.core.track import FaceTrack, TrackPoint

REPO = Path(__file__).resolve().parents[1]
ALL_PRESETS = available()
FRAMES = [(1080, 1920), (720, 1280)]
STEP = 0.35  # одно слово на треть секунды: обычный темп речи
TEXTS = ["бот", "добавляет", "звуки", "и", "картинки", "сам", "без", "монтажёра", "быстро"]


def cfg(**kw) -> Settings:
    base = dict(sub_tail=0.15, render_fps=30, render_width=1080, render_height=1920)
    return Settings(_env_file=None, bot_token="x", **{**base, **kw})


def talk(n: int = 90, preset: str = "pulse", **kw) -> EDL:
    """Ровная речь: слово `i` звучит с `i * STEP`, блоки по три слова."""
    words = [
        Word(
            idx=i,
            text=TEXTS[i % len(TEXTS)],
            start=round(i * STEP, 3),
            end=round(i * STEP + 0.3, 3),
        )
        for i in range(n)
    ]
    captions = [
        CaptionBlock(words=[CaptionWord(word_idx=j) for j in range(i, min(i + 3, n))])
        for i in range(0, n, 3)
    ]
    data = dict(source_duration=n * STEP, words=words, cuts=[], captions=captions, preset=preset)
    data.update(kw)
    return EDL(**data)


def head(seconds: float, fps: int = 30, *, cx=0.5, cy=0.42, h=0.22, sway=0.015) -> FaceTrack:
    """Трек говорящей головы: стоит на месте и слегка покачивается."""
    return FaceTrack(
        fps=fps,
        points=[
            TrackPoint(
                f=f,
                cx=cx + sway * math.sin(f / 17),
                cy=cy + sway * math.cos(f / 23),
                h=h,
                conf=0.9,
            )
            for f in range(int(seconds * fps) + 2)
        ],
    )


def dress(edl: EDL, visuals, **kw) -> RenderPlan:
    args = dict(apply_cuts=False, cfg=cfg())
    args.update(kw)
    return dressed_plan(build_plan, edl, "video.mp4", visuals=visuals, **args)


def visuals_of(plan: RenderPlan) -> dict:
    return plan.meta.get("visuals") or {"overlays": [], "scenes": []}


def scene(layout: str = "card_top", word_from: int = 9, word_to: int = 17, **kw) -> Scene:
    return Scene(layout=layout, word_from=word_from, word_to=word_to, **kw)


def chip(word: int, text: str = "9 500 000 ₽", **kw) -> Overlay:
    return Overlay(kind="chip", word_idx=word, text=text, **kw)


def rect_of(data: dict) -> Rect:
    return Rect.from_dict(data["rect"])


def box_of(item: dict) -> Rect:
    """Место элемента с наклоном и покачиванием — то, что видно в кадре."""
    return hit_box(rect_of(item), item["rotate"], item["float_px"])


@pytest.fixture
def photo(tmp_path: Path) -> str:
    from PIL import Image

    path = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), (200, 120, 40)).save(path)
    return str(path)


def rich_sheet(photo: str) -> Visuals:
    """Лист со всеми приёмами разом: по нему меряются общие правила."""
    return Visuals(
        overlays=[
            chip(24, emoji="💰", until_word_idx=30),
            Overlay(kind="sticker", word_idx=32, sticker="rocket"),
            chip(55, "во время вставки", style="pill"),
            Overlay(kind="decor", word_idx=60, glyph="question"),
            chip(66, "−30 %", style="tag"),
            chip(72, "факт", style="label"),
            Overlay(kind="sticker", word_idx=73, emoji="🔥"),
        ],
        scenes=[
            scene(
                "paper",
                9,
                17,
                heading="одна цель",
                items=[
                    SceneItem(kind="sticker", word_idx=11, emoji="🏃"),
                    SceneItem(kind="photo", word_idx=13, asset=photo, text="офис"),
                    SceneItem(kind="chip", word_idx=17, text="одна цель"),
                ],
            ),
            scene(
                "card_top",
                40,
                48,
                heading="где это",
                items=[
                    SceneItem(kind="sticker", word_idx=42, sticker="calendar"),
                    SceneItem(kind="map", word_idx=44, text="здесь"),
                    SceneItem(kind="chip", word_idx=48, text="сегодня"),
                ],
            ),
            scene(
                "phone",
                78,
                86,
                heading="в телефоне",
                items=[
                    SceneItem(kind="chip", word_idx=80, text="запись"),
                    SceneItem(kind="sticker", word_idx=82, emoji="📱"),
                    SceneItem(kind="map", word_idx=84),
                ],
            ),
        ],
    )


def rich_edl(preset: str = "pulse") -> EDL:
    edl = talk(90, preset)
    edl.broll = [Broll(word_idx=54, duration=2.0, query="q", anim="pop_in", anchor="card")]
    return edl


# ------------------------------------------------------------------ без графики


def test_a_sheet_without_visuals_gives_the_plain_plan():
    """Нет графики — план ровно тот, что собрал бы `build_plan`, до последнего поля."""
    edl = talk(30)
    plain = build_plan(edl, "video.mp4", apply_cuts=False, cfg=cfg()).to_dict()
    for sheet in (None, Visuals(), {"version": 1, "overlays": [], "scenes": []}):
        plan = dress(edl, sheet)
        assert plan.to_dict() == plain
        assert "visuals" not in plan.meta


def test_a_sheet_where_nothing_survives_leaves_the_plan_alone():
    """Всё выпало — в плане не остаётся ни ключа графики, ни переписанного блока."""
    edl = talk(30)
    plain = build_plan(edl, "video.mp4", apply_cuts=False, cfg=cfg()).to_dict()
    sheet = Visuals(
        overlays=[chip(29), Overlay(kind="sticker", word_idx=5, sticker="нет-такого")],
        scenes=[scene("paper", 1, 6)],  # в первые секунды ролика сцены не бывает
    )
    assert dress(edl, sheet).to_dict() == plain


def test_a_malformed_sheet_costs_the_graphics_not_the_reel():
    """Лист не по форме — ролик выходит без графики, а не падает на сборке плана."""
    edl = talk(30)
    plain = build_plan(edl, "video.mp4", apply_cuts=False, cfg=cfg()).to_dict()
    broken = {"version": 1, "overlays": [{"kind": "laser", "word_idx": 9}], "scenes": []}
    assert dress(edl, broken).to_dict() == plain
    window_of = overlays._word_windows(edl, edl.timeline())
    assert sfx_events(broken, window_of, edl.out_duration) == []


def test_the_sheet_is_read_from_the_edl_when_not_passed():
    """Без явного листа графика берётся из самого листа ролика (`EDL.visuals`)."""
    edl = talk(30)
    edl.visuals = Visuals(overlays=[chip(9)])
    plan = dressed_plan(build_plan, edl, "video.mp4", apply_cuts=False, cfg=cfg())
    assert [o["kind"] for o in visuals_of(plan)["overlays"]] == ["chip"]


def test_the_inputs_are_not_touched(photo):
    """Ни лист ролика, ни лист графики не меняются: копия блоков живёт один вызов."""
    edl, sheet = rich_edl(), rich_sheet(photo)
    before = (edl.model_dump(), sheet.model_dump())
    dress(edl, sheet, track=head(32))
    assert (edl.model_dump(), sheet.model_dump()) == before


# ------------------------------------------------------------------ что выпадает


def test_an_overlay_on_a_cut_word_is_dropped():
    edl = talk(
        30,
        cuts=[
            Cut(src_in=0.0, src_out=3.4, reason="keep"),
            Cut(src_in=3.4, src_out=4.6, reason="drop_silence"),
            Cut(src_in=4.6, src_out=10.5, reason="keep"),
        ],
    )
    # Слова 10-12 лежат в вырезанном куске (3.5-4.5 с).
    sheet = Visuals(overlays=[chip(11), chip(18, "жив")])
    plan = dress(edl, sheet, apply_cuts=True)
    assert [o["words"] for o in visuals_of(plan)["overlays"]] == [["жив"]]


def test_an_overlay_at_the_very_end_is_dropped():
    """До конца ролика не остаётся минимального срока — плашка была бы вспышкой."""
    plan = dress(talk(30), Visuals(overlays=[chip(28), chip(12, "жив")]))
    assert [o["words"] for o in visuals_of(plan)["overlays"]] == [["жив"]]


@pytest.mark.parametrize(
    "word_from, word_to, why",
    [
        (9, 12, "короче минимума"),
        (9, 30, "длиннее максимума"),
        (3, 12, "в первые секунды ролика"),
        (20, 29, "в последнюю секунду ролика"),
    ],
)
def test_a_scene_outside_its_time_rules_is_dropped(word_from, word_to, why):
    plan = dress(talk(30), Visuals(scenes=[scene("card_top", word_from, word_to)]))
    assert visuals_of(plan)["scenes"] == [], why


def test_a_scene_over_the_title_card_is_dropped():
    preset = load_preset("pulse").model_copy(deep=True)
    preset.caption.title_card.start = 3.5  # титр попадает на время сцены
    edl = talk(30, title="Главная мысль")
    sheet = Visuals(scenes=[scene("card_top", 9, 17)])
    assert visuals_of(dress(edl, sheet, preset=preset))["scenes"] == []
    # Тот же лист без заголовка — титра нет, и сцене ничто не мешает.
    assert len(visuals_of(dress(talk(30), sheet, preset=preset))["scenes"]) == 1


def test_a_scene_over_a_picture_is_dropped():
    edl = talk(30)
    edl.broll = [Broll(word_idx=12, duration=1.5, query="q", anim="pop_in", anchor="card")]
    assert visuals_of(dress(edl, Visuals(scenes=[scene("paper", 9, 17)])))["scenes"] == []


def test_a_scene_over_a_list_is_dropped():
    edl = talk(30)
    for at in (4, 5):  # блоки 4 и 5 — слова 12-17 — пункты списка
        edl.captions[at] = edl.captions[at].model_copy(update={"item": True, "emoji": "🎵"})
    assert visuals_of(dress(edl, Visuals(scenes=[scene("card_top", 9, 17)])))["scenes"] == []


def test_a_split_frame_has_no_scenes_and_no_graphics_over_the_split():
    edl = talk(60, split_until=6.0)
    sheet = Visuals(
        overlays=[chip(9, "в шве"), chip(30, "после")],
        scenes=[scene("card_top", 24, 32)],
    )
    made = visuals_of(dress(edl, sheet))
    assert made["scenes"] == []
    assert [o["words"] for o in made["overlays"]] == [["после"]]


def test_of_two_overlapping_scenes_the_first_stays():
    sheet = Visuals(scenes=[scene("paper", 9, 17), scene("card_top", 15, 24)])
    made = visuals_of(dress(talk(60), sheet))
    assert [s["layout"] for s in made["scenes"]] == ["paper"]


def test_a_free_overlay_during_a_scene_is_dropped():
    """Во время сцены в кадре живёт только её содержимое."""
    sheet = Visuals(
        overlays=[
            chip(12, "в сцене"),
            chip(7, "задел начало", until_word_idx=10),
            chip(30, "после"),
        ],
        scenes=[scene("paper", 9, 17)],
    )
    made = visuals_of(dress(talk(60), sheet))
    assert [o["words"] for o in made["overlays"]] == [["после"]]


def test_an_overlay_with_nothing_to_draw_is_dropped():
    sheet = Visuals(
        overlays=[
            Overlay(kind="chip", word_idx=9),
            Overlay(kind="chip", word_idx=12, text="   "),
            Overlay(kind="sticker", word_idx=15),
            Overlay(kind="sticker", word_idx=18, sticker="нет-такого"),
            Overlay(kind="sticker", word_idx=21, emoji="не эмодзи"),
            Overlay(kind="decor", word_idx=24),
            chip(40, "жив"),
        ],
    )
    made = visuals_of(dress(talk(60), sheet))
    assert [o["words"] for o in made["overlays"]] == [["жив"]]


def test_a_photo_without_a_file_is_dropped_and_the_scene_stays(photo):
    sheet = Visuals(
        scenes=[
            scene(
                "card_top",
                9,
                17,
                items=[
                    SceneItem(kind="photo", word_idx=10),
                    SceneItem(kind="photo", word_idx=12, asset=photo),
                ],
            )
        ]
    )
    (made,) = visuals_of(dress(talk(30), sheet))["scenes"]
    assert [i["kind"] for i in made["items"]] == ["photo"]
    assert made["items"][0]["asset"] == photo


def test_an_overlay_with_no_free_slot_is_dropped():
    """Полнокадровая вставка занимает всё — графике встать негде, и она уступает."""
    edl = talk(60)
    edl.broll = [Broll(word_idx=20, duration=3.0, query="q", anim="pop_in", anchor="fullscreen")]
    sheet = Visuals(overlays=[chip(21, "под вставкой"), chip(40, "жив")])
    made = visuals_of(dress(edl, sheet))
    assert [o["words"] for o in made["overlays"]] == [["жив"]]


def test_a_chip_too_long_for_the_frame_is_dropped():
    """Плашка ужимается до пола своего вида, а дальше выпадает: мелочь не приём."""
    edl = talk(60, "bold")
    sheet = Visuals(overlays=[chip(20, "ЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩЩ"), chip(40, "жив")])
    made = visuals_of(dress(edl, sheet))
    assert [o["words"] for o in made["overlays"]] == [["ЖИВ"]]


# ------------------------------------------------------------------ субтитры в сцене


def staged_edl() -> EDL:
    """Блоки, которым в сцене не место: тихий наверху, со стеклом, за головой."""
    edl = talk(30)
    edl.captions[3] = edl.captions[3].model_copy(update={"position": "top", "quiet": True})
    edl.captions[4] = edl.captions[4].model_copy(update={"panel": True, "emoji": "🎯"})
    edl.captions[5] = edl.captions[5].model_copy(
        update={"anchor_track": AnchorTrack(), "position": "center"}
    )
    return edl


def blocks_in(plan: RenderPlan, first: int, last: int):
    return [
        b
        for b in plan.blocks
        if any(first <= w.word_idx <= last for w in b.words) and not b.title_card
    ]


@pytest.mark.parametrize("layout", ["card_top", "paper", "phone"])
def test_captions_inside_a_scene_stand_at_the_bottom_as_posters(layout):
    edl = staged_edl()
    track = head(11)
    plain = build_plan(edl, "video.mp4", apply_cuts=False, cfg=cfg(), track=track)
    assert {b.position for b in blocks_in(plain, 9, 17)} != {"bottom"}  # иначе тест пуст
    plan = dress(edl, Visuals(scenes=[scene(layout, 9, 17)]), track=track)
    inside = blocks_in(plan, 9, 17)
    assert len(inside) == 3
    for block in inside:
        assert block.position == "bottom"
        assert not block.quiet and not block.panel
        assert block.anchor_path is None and block.icon is None and block.pill is None
    # Слова никуда не делись и идут в том же порядке.
    assert [w.word_idx for b in plan.blocks for w in b.words] == [
        w.word_idx for b in plain.blocks for w in b.words
    ]
    # Блоки вне сцены стоят как стояли.
    outside = [b for b in plan.blocks if b not in inside]
    assert [b.position for b in outside] == [
        b.position for b in plain.blocks if not any(9 <= w.word_idx <= 17 for w in b.words)
    ]


def test_the_luma_under_a_scene_caption_is_the_luma_of_the_scene_background():
    edl = talk(30)
    for block in edl.captions:
        block.bg_luma = 0.33  # замер по видео

    paper = dress(edl, Visuals(scenes=[scene("paper", 9, 17)]))
    for block in blocks_in(paper, 9, 17):
        assert block.bg_luma == pytest.approx(luminance(overlays.PAPER_COLOR), abs=1e-3)
        assert block.bg_luma > 0.8

    board = dress(edl, Visuals(scenes=[scene("card_top", 9, 17)]))
    for block in blocks_in(board, 9, 17):
        assert block.bg_luma < 0.03

    # У телефона субтитр лежит поверх самого видео: замер остаётся в силе.
    phone = dress(edl, Visuals(scenes=[scene("phone", 9, 17)]))
    assert {b.bg_luma for b in phone.blocks} == {0.33}
    # И вне сцены замер никто не трогает.
    assert {b.bg_luma for b in paper.blocks if b not in blocks_in(paper, 9, 17)} == {0.33}


def test_a_block_that_only_touches_the_scene_keeps_the_measured_luma():
    """Блок, задевший сцену одним словом, почти всю жизнь лежит на видео."""
    edl = talk(30)
    for block in edl.captions:
        block.bg_luma = 0.33
    # Сцена кончается на слове 18 — первом слове блока 18-20.
    plan = dress(edl, Visuals(scenes=[scene("paper", 10, 18)]))
    tail = next(b for b in plan.blocks if b.words[0].word_idx == 18)
    assert tail.position == "bottom" and tail.bg_luma == 0.33


# ------------------------------------------------------------------ место в кадре


def caption_zone_of(block) -> Rect:
    rect = block.rect
    if not block.anchor_path:
        return rect
    xs = [0.0, *(p[0] for p in block.anchor_path)]
    ys = [0.0, *(p[1] for p in block.anchor_path)]
    return Rect(
        rect.x + min(xs), rect.y + min(ys),
        rect.width + max(xs) - min(xs), rect.height + max(ys) - min(ys),
    )


def face_boxes(track: FaceTrack, start: float, end: float, frame) -> list[Rect]:
    """Лицо от глаз до подбородка на каждом кадре — своим расчётом, не модуля."""
    width, height = frame
    out = []
    for f in range(round(start * track.fps), round(end * track.fps) + 1):
        p = track.at(f)
        if p is None or p.conf < FACE_MIN_CONF:
            continue
        face_h = p.h * height
        half = FACE_WIDTH_RATIO * face_h / 2
        out.append(
            Rect(
                p.cx * width - half,
                p.cy * height - FACE_GUARD_UP * face_h,
                2 * half,
                (FACE_GUARD_UP + FACE_GUARD_DOWN) * face_h,
            )
        )
    return out


def assert_inside_safe_area(box: Rect, plan: RenderPlan, what: str) -> None:
    ratio = plan.preset["geometry"]["safe_area_ratio"]
    sx, sy = plan.width * ratio, plan.height * ratio
    assert box.x >= sx - 0.01 and box.right <= plan.width - sx + 0.01, (what, box)
    assert box.y >= sy - 0.01 and box.bottom <= plan.height - sy + 0.01, (what, box)


def assert_overlays_yield(plan: RenderPlan, track: FaceTrack) -> None:
    """Свободные элементы не лежат ни на субтитре, ни на вставке, ни на лице."""
    frame = (plan.width, plan.height)
    for item in visuals_of(plan)["overlays"]:
        boxes = (
            [mark_box(mark) for mark in item["marks"]]
            if item["kind"] == "decor"
            else [box_of(item)]
        )
        what = (plan.preset["name"], frame, item["kind"], item["words"])
        start, end = item["start"], item["end"]
        for box in boxes:
            assert_inside_safe_area(box, plan, what)
            for block in plan.blocks:
                if block.start < end and block.end > start:
                    assert not box.overlaps(caption_zone_of(block)), (what, "субтитр")
            for picture in plan.broll:
                if picture.start < end and picture.end > start:
                    assert not box.overlaps(picture.rect), (what, "вставка")
            for face in face_boxes(track, start, end, frame):
                assert not box.overlaps(face), (what, "лицо")


def assert_scene_is_tidy(plan: RenderPlan, made: dict) -> None:
    """Содержимое сцены не лежит на окне, субтитре и друг на друге."""
    fps = plan.fps
    window = hit_box(Rect.from_dict(made["window"]).inset(-made["bezel"]), made["tilt"])
    rest_from = made["start"] + made["in_frames"] / fps
    rest_to = made["end"] - made["out_frames"] / fps
    parts = [*made["items"], *([made["heading"]] if made["heading"] else [])]
    for n, item in enumerate(parts):
        what = (plan.preset["name"], made["layout"], item["kind"], item["style"], item["words"])
        box = box_of(item)
        assert_inside_safe_area(box, plan, what)
        assert not box.overlaps(window), (what, "окно")
        # Содержимое живёт, только пока окно стоит на месте.
        assert item["start"] >= rest_from - 1e-3 and item["end"] <= rest_to + 1e-3, what
        for block in plan.blocks:
            if block.start < item["end"] and block.end > item["start"]:
                assert not box.overlaps(caption_zone_of(block)), (what, "субтитр")
        for other in parts[n + 1 :]:
            # Подпись карты лежит на своей карте — это одна вещь.
            pair = {item["kind"], other["style"] or other["kind"]}
            if pair == {"map", "label"} and box.overlaps(box_of(other)):
                inner = box_of(other) if item["kind"] == "map" else box
                outer = box if item["kind"] == "map" else box_of(other)
                assert inner.x >= outer.x and inner.right <= outer.right, what
                continue
            assert not box.overlaps(box_of(other)), (what, other["kind"], other["words"])


@pytest.mark.parametrize("frame", FRAMES, ids=lambda f: f"{f[0]}x{f[1]}")
@pytest.mark.parametrize("name", ALL_PRESETS)
def test_graphics_yield_to_captions_pictures_and_the_face(name, frame, photo):
    """Главное правило, на каждой теме и на двух размерах кадра.

    Ни один выживший прямоугольник не пересекает субтитр, вставку и лицо и не
    выходит из безопасной зоны; содержимое сцены не лежит ни на окне
    говорящего, ни друг на друге.
    """
    track = head(32)
    plan = dress(
        rich_edl(name), rich_sheet(photo), track=track, width=frame[0], height=frame[1]
    )
    made = visuals_of(plan)
    assert_overlays_yield(plan, track)
    for staged in made["scenes"]:
        assert_scene_is_tidy(plan, staged)

    # Проверка не пуста: сцены на месте, в каждой есть содержимое, а из
    # свободных элементов выжило больше половины.
    assert [s["layout"] for s in made["scenes"]] == ["paper", "card_top", "phone"], name
    for staged in made["scenes"]:
        assert staged["heading"] is not None, (name, staged["layout"])
        assert len(staged["items"]) >= 2, (name, staged["layout"])
    kinds = [o["kind"] for o in made["overlays"]]
    assert len(kinds) >= 4 and {"chip", "sticker", "decor"} <= set(kinds), (name, kinds)


@pytest.mark.parametrize("cy, h", [(0.3, 0.16), (0.42, 0.22), (0.55, 0.3), (0.65, 0.47)])
def test_graphics_stay_off_the_face_wherever_it_is(cy, h, photo):
    """Общий план, обычный и крупный: место ищется по треку, а не по привычке."""
    track = head(32, cy=cy, h=h)
    plan = dress(rich_edl(), rich_sheet(photo), track=track)
    assert_overlays_yield(plan, track)
    assert len(visuals_of(plan)["overlays"]) >= 3, (cy, h)


def test_graphics_yield_to_the_whole_road_of_a_caption_that_rides_the_head():
    """Блок, едущий за головой, занимает не прямоугольник покоя, а всю свою дорогу."""
    edl = talk(60)
    edl.captions[7] = edl.captions[7].model_copy(update={"anchor_track": AnchorTrack()})
    track = head(22, sway=0.06)
    sheet = Visuals(
        overlays=[
            chip(21, "над головой", slot="above_head"),
            Overlay(kind="sticker", word_idx=22, emoji="🔥", slot="above_head"),
        ]
    )
    plan = dress(edl, sheet, track=track)
    rider = next(b for b in plan.blocks if b.words[0].word_idx == 21)
    assert rider.anchor_path, "блок не поехал за головой — тест пуст"
    road = caption_zone_of(rider)
    assert road.width > rider.rect.width or road.height > rider.rect.height
    assert_overlays_yield(plan, track)

    # Проверка не пуста: без дороги те же элементы встают, и хотя бы один из
    # них лёг бы на дорогу. Значит, с дорогой им пришлось уступить — сдвинуться
    # или выпасть (на этом кадре дорога закрывает почти весь кадр, и они выпадают).
    still = dress(talk(60), sheet, track=track)
    placed = [box_of(item) for item in visuals_of(still)["overlays"]]
    assert len(placed) == 2
    assert any(box.overlaps(road) for box in placed)


def test_two_overlays_in_the_frame_at_once_do_not_lie_on_each_other():
    sheet = Visuals(
        overlays=[
            chip(20, "первая", slot="above_head"),
            chip(21, "вторая", slot="above_head"),
            Overlay(kind="sticker", word_idx=22, emoji="🔥", slot="above_head"),
            Overlay(kind="decor", word_idx=23, glyph="sparkle"),
        ]
    )
    made = visuals_of(dress(talk(60), sheet, track=head(22)))["overlays"]
    assert len(made) >= 3
    boxes = [
        box
        for item in made
        for box in (
            [mark_box(m) for m in item["marks"]] if item["kind"] == "decor" else [box_of(item)]
        )
    ]
    for n, box in enumerate(boxes):
        for other in boxes[n + 1 :]:
            assert not box.overlaps(other)


def test_the_requested_slot_is_honoured_when_it_is_free():
    track = head(22)
    plan = dress(
        talk(60),
        Visuals(
            overlays=[
                chip(20, "слева", slot="top_left"),
                chip(30, "справа", slot="top_right"),
                chip(40, "над", slot="above_head"),
                chip(50, "грудь", slot="chest"),
            ]
        ),
        track=track,
    )
    left, right, above, chest = (rect_of(o) for o in visuals_of(plan)["overlays"])
    face = face_at(track, 14.0, 15.0, (1080, 1920))
    assert left.center_x < 1080 / 2 < right.center_x
    assert left.y < 0.3 * 1920 and right.y < 0.3 * 1920
    assert above.bottom < face.box.y and abs(above.center_x - face.cx) < 60
    # Грудь занята субтитром (он внизу), и плашка ушла в другой слот, а не выпала.
    assert chest.bottom < face.box.y or chest.y > face.box.bottom


# ------------------------------------------------------------------ время


def timed(made: dict) -> list[dict]:
    out = list(made["overlays"])
    for staged in made["scenes"]:
        out += [staged, *staged["items"], *([staged["heading"]] if staged["heading"] else [])]
    return out


@pytest.mark.parametrize("fps", [24, 30, 60])
def test_every_time_is_on_the_frame_grid(fps, photo):
    plan = dress(rich_edl(), rich_sheet(photo), track=head(32, fps), fps=fps)
    parts = timed(visuals_of(plan))
    assert len(parts) > 15
    for part in parts:
        for key in ("start", "end"):
            frames = part[key] * fps
            # В плане секунды округлены до тысячных: это сотые доли кадра.
            assert abs(frames - round(frames)) < 0.05, (part.get("kind"), key, part[key])
        life = round(part["end"] * fps) - round(part["start"] * fps)
        for key in ("in_frames", "out_frames"):
            assert MIN_ANIM_FRAMES <= part[key] <= life / 3, (part.get("kind"), key, life)


def test_scene_items_enter_on_their_word_but_only_once_the_stage_has_settled():
    sheet = Visuals(
        scenes=[
            scene(
                "card_top",
                9,
                20,
                items=[
                    SceneItem(kind="chip", word_idx=9, text="рано"),
                    SceneItem(kind="chip", word_idx=14, text="вовремя"),
                    SceneItem(kind="chip", word_idx=20, text="поздно"),
                ],
            )
        ]
    )
    plan = dress(talk(40), sheet, fps=30)
    (made,) = visuals_of(plan)["scenes"]
    early, timely, late = made["items"]
    settled = made["start"] + made["in_frames"] / 30
    leaving = made["end"] - made["out_frames"] / 30
    # Слово звучит раньше, чем окно доехало: пункт ждёт окна.
    assert early["start"] == pytest.approx(settled, abs=1e-3)
    # Обычный случай: ровно на своём слове.
    assert timely["start"] == pytest.approx(14 * STEP, abs=1 / 30)
    # Слово у самого конца сцены: пункт входит раньше слова, но успевает постоять.
    assert late["start"] < 20 * STEP
    assert late["end"] - late["start"] >= overlays.ITEM_MIN_LIFE_S - 1e-3
    for item in made["items"]:
        assert item["end"] == pytest.approx(leaving, abs=1e-3)


def test_items_squeezed_to_the_end_of_a_scene_still_arrive_one_by_one():
    sheet = Visuals(
        scenes=[
            scene(
                "card_top",
                9,
                17,
                items=[
                    SceneItem(kind="chip", word_idx=16, text="раз"),
                    SceneItem(kind="chip", word_idx=17, text="два"),
                    SceneItem(kind="chip", word_idx=17, text="три"),
                ],
            )
        ]
    )
    (made,) = visuals_of(dress(talk(30), sheet))["scenes"]
    starts = [item["start"] for item in made["items"]]
    assert [i["words"] for i in made["items"]] == [["раз"], ["два"], ["три"]]
    assert starts == sorted(starts) and len(set(starts)) == 3


def test_items_whose_words_came_before_the_stage_settled_do_not_arrive_in_one_frame():
    sheet = Visuals(
        scenes=[
            scene(
                "card_top",
                9,
                20,
                items=[
                    SceneItem(kind="chip", word_idx=9, text="раз"),
                    SceneItem(kind="chip", word_idx=9, text="два"),
                    SceneItem(kind="chip", word_idx=10, text="три"),
                ],
            )
        ]
    )
    (made,) = visuals_of(dress(talk(40), sheet, fps=30))["scenes"]
    starts = [item["start"] for item in made["items"]]
    assert starts[0] == pytest.approx(made["start"] + made["in_frames"] / 30, abs=1e-3)
    assert all(b - a >= 0.1 for a, b in zip(starts, starts[1:])), starts


def test_two_runs_give_byte_equal_json(photo):
    """Перерендер сохранённого листа даёт то же видео: ни часов, ни случайности."""
    runs = [
        json.dumps(
            dress(rich_edl(), rich_sheet(photo), track=head(32)).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        for _ in range(2)
    ]
    assert runs[0] == runs[1]
    assert json.loads(runs[0])["meta"]["visuals"]["overlays"]


def test_the_seed_comes_from_the_place_in_the_list_unless_the_sheet_sets_it():
    sheet = Visuals(
        overlays=[
            chip(20, "раз", style="tag"),
            chip(30, "два", style="tag"),
            chip(40, "три", style="tag", seed=41),
        ]
    )
    made = visuals_of(dress(talk(60), sheet))["overlays"]
    assert [o["seed"] for o in made] == [1, 2, 41]
    # Наклон ценника: знак — от зерна, величина — несколько градусов.
    tilts = [o["rotate"] for o in made]
    assert all(2.0 <= abs(t) <= 6.0 for t in tilts)
    assert tilts[0] < 0 < tilts[1]


def test_decor_is_laid_out_from_the_seed():
    def marks(seed: int) -> list[dict]:
        sheet = Visuals(overlays=[Overlay(kind="decor", word_idx=20, glyph="question", seed=seed)])
        (made,) = visuals_of(dress(talk(60), sheet, track=head(22)))["overlays"]
        return made["marks"]

    first, again, other = marks(7), marks(7), marks(8)
    assert first == again and first != other
    assert len(first) >= 3
    # Знаки разные: кегль, поворот, прозрачность и фаза покачивания.
    for key in ("size", "rotate", "opacity", "phase"):
        assert len({mark[key] for mark in first}) > 1, key
    assert all(0 < mark["opacity"] <= 1 and 0 <= mark["phase"] <= 1 for mark in first)


# ------------------------------------------------------------------ окно сцены


def scenes_of_every_layout(track: FaceTrack | None, frame=(1080, 1920)) -> tuple[RenderPlan, list]:
    sheet = Visuals(
        scenes=[scene("paper", 9, 17), scene("card_top", 40, 48), scene("phone", 78, 86)]
    )
    plan = dress(talk(90), sheet, track=track, width=frame[0], height=frame[1])
    return plan, visuals_of(plan)["scenes"]


@pytest.mark.parametrize("frame", FRAMES, ids=lambda f: f"{f[0]}x{f[1]}")
@pytest.mark.parametrize(
    "face",
    [None, (0.5, 0.42, 0.22), (0.2, 0.3, 0.16), (0.8, 0.6, 0.3), (0.46, 0.65, 0.47)],
    ids=["no-track", "middle", "left", "right-low", "close-up"],
)
def test_the_scaled_video_covers_the_scene_window_on_every_frame(face, frame):
    """Под окном говорящего нет щели — ни в покое, ни на одном кадре переезда."""
    track = head(32, cx=face[0], cy=face[1], h=face[2]) if face else None
    plan, made = scenes_of_every_layout(track, frame)
    width, height = frame
    assert len(made) == 3
    for staged in made:
        first, last = round(staged["start"] * plan.fps), round(staged["end"] * plan.fps)
        for f in range(first, last + 1):
            at = stage_at(staged, f, plan.fps, width, height)
            what = (staged["layout"], f, at)
            assert at["tx"] <= 1e-9 and at["ty"] <= 1e-9, what
            assert at["tx"] + at["scale"] * width >= at["width"] - 1e-6, what
            assert at["ty"] + at["scale"] * height >= at["height"] - 1e-6, what


@pytest.mark.parametrize(
    "face",
    [(0.5, 0.42, 0.22), (0.25, 0.3, 0.16), (0.75, 0.6, 0.3), (0.46, 0.65, 0.47)],
    ids=["middle", "left", "right-low", "close-up"],
)
def test_the_face_stays_inside_the_scene_window(face):
    """Лицо от глаз до подбородка видно в окне, где бы оно ни стояло в кадре."""
    cx, cy, h = face
    track = head(32, cx=cx, cy=cy, h=h, sway=0.0)
    plan, made = scenes_of_every_layout(track)
    width, height = plan.width, plan.height
    for staged in made:
        window = Rect.from_dict(staged["window"])
        scale, tx, ty = staged["scale"], staged["tx"], staged["ty"]
        face_h = h * height
        left = window.x + tx + scale * (cx * width - FACE_WIDTH_RATIO * face_h / 2)
        right = window.x + tx + scale * (cx * width + FACE_WIDTH_RATIO * face_h / 2)
        top = window.y + ty + scale * (cy * height - FACE_GUARD_UP * face_h)
        bottom = window.y + ty + scale * (cy * height + FACE_GUARD_DOWN * face_h)
        what = (staged["layout"], face)
        assert window.x <= left and right <= window.right, what
        assert window.y <= top and bottom <= window.bottom, what


def test_each_layout_is_what_it_says():
    plan, made = scenes_of_every_layout(head(32))
    paper, card, phone = made
    width, height = plan.width, plan.height

    # Карточка наверху: окно во всю ширину в верхней половине, под ним тёмная доска.
    window = Rect.from_dict(card["window"])
    assert window.width > 0.85 * width and window.bottom < 0.5 * height
    assert card["radius"] == pytest.approx(0.05 * width, abs=2) and card["tilt"] == 0
    assert card["bg"]["kind"] in ("board", "gradient")
    assert luminance(card["bg"]["color"]) < 0.05 and luminance(card["bg"]["color2"]) < 0.05

    # Лист в клетку: светлый фон, окно — наклонённая карточка справа.
    window = Rect.from_dict(paper["window"])
    assert paper["bg"]["kind"] == "paper" and paper["bg"]["grid"] > 0
    assert luminance(paper["bg"]["color"]) > 0.7
    assert window.center_x > 0.6 * width and 1 <= abs(paper["tilt"]) <= 5
    assert paper["radius"] > 0

    # Телефон: экран 9:16 по центру, рамка, размытый кадр под ним.
    window = Rect.from_dict(phone["window"])
    assert phone["chrome"] == "phone" and phone["bezel"] > 0
    assert phone["bg"]["kind"] == "still"
    assert window.width / window.height == pytest.approx(width / height, abs=1e-3)
    assert window.center_x == pytest.approx(width / 2, abs=1)
    assert phone["scale"] == pytest.approx(window.width / width, abs=1e-3)


def test_a_scene_heading_is_big_boxless_text():
    sheet = Visuals(scenes=[scene("paper", 9, 17, heading="одна цель")])
    (made,) = visuals_of(dress(talk(30), sheet))["scenes"]
    title = made["heading"]
    assert title["style"] == "heading" and title["words"] == ["ОДНА", "ЦЕЛЬ"]
    assert title["fill_opacity"] == 0 and title["padding_x"] == 0 and title["padding_y"] == 0
    assert title["font"] == "caption" and title["font_size"] > 0.04 * 1920
    # На светлой бумаге заголовок тёмный, на тёмной доске — светлый.
    assert luminance(title["color"]) < 0.1
    sheet = Visuals(scenes=[scene("card_top", 9, 17, heading="одна цель")])
    (made,) = visuals_of(dress(talk(30), sheet))["scenes"]
    assert luminance(made["heading"]["color"]) > 0.7


def test_a_map_carries_a_pin_and_its_text_becomes_a_label_next_to_it():
    sheet = Visuals(
        scenes=[scene("card_top", 9, 17, items=[SceneItem(kind="map", word_idx=12, text="здесь")])]
    )
    (made,) = visuals_of(dress(talk(30), sheet))["scenes"]
    card, label = made["items"]
    assert card["kind"] == "map" and card["seed"] > 0
    spot = rect_of(card)
    assert 0 < card["pin"]["x"] < spot.width and 0 < card["pin"]["y"] < spot.height
    assert label["kind"] == "chip" and label["style"] == "label" and label["words"] == ["ЗДЕСЬ"]
    # Подпись лежит на карте и не дальше высоты карты от точки.
    tag = rect_of(label)
    assert spot.x <= tag.x and tag.right <= spot.right
    assert spot.y <= tag.y and tag.bottom <= spot.bottom
    pin = (spot.x + card["pin"]["x"], spot.y + card["pin"]["y"])
    assert abs(tag.center_x - pin[0]) < spot.width / 2
    assert 0 <= tag.y - pin[1] < spot.height / 2


# ------------------------------------------------------------------ формулы движения


def plan_scene(**kw) -> PlanScene:
    data = dict(
        layout="paper",
        start=2.0,
        end=5.0,
        in_frames=12,
        out_frames=9,
        window=Rect(420.0, 300.0, 560.0, 760.0),
        radius=44.0,
        tilt=3.0,
        scale=0.6,
        tx=-20.0,
        ty=-130.0,
        bg=PlanSceneBg(kind="paper"),
    )
    data.update(kw)
    return PlanScene(**data)


def test_stage_is_the_full_frame_at_both_ends_and_the_window_at_rest():
    made = plan_scene()
    fps, width, height = 30, 1080, 1920
    first, last = 60, 150
    full = dict(p=0.0, x=0.0, y=0.0, width=1080.0, height=1920.0, radius=0.0, tilt=0.0,
                scale=1.0, tx=0.0, ty=0.0)
    rest = dict(p=1.0, x=420.0, y=300.0, width=560.0, height=760.0, radius=44.0, tilt=3.0,
                scale=0.6, tx=-20.0, ty=-130.0)
    for frame in (0, first - 1, first, last, last + 1, 10_000):
        assert stage_at(made, frame, fps, width, height) == pytest.approx(full), frame
    # Вход кончается через `in_frames` кадров, выход НАЧИНАЕТСЯ за `out_frames` до конца.
    for frame in (first + 12, first + 40, last - 9):
        assert stage_at(made, frame, fps, width, height) == pytest.approx(rest), frame
    assert 0 < stage_at(made, first + 11, fps, width, height)["p"] < 1
    assert 0 < stage_at(made, last - 8, fps, width, height)["p"] < 1


def test_stage_moves_by_smoothstep_and_never_goes_backwards():
    made = plan_scene()
    fps = 30
    ps = [stage_at(made, f, fps, 1080, 1920)["p"] for f in range(60, 151)]
    rise, fall = ps[:13], ps[-10:]
    assert rise == sorted(rise) and fall == sorted(fall, reverse=True)
    # Ровно та кривая, что записана в контракте: t²(3 − 2t).
    t = 5 / 12
    assert ps[5] == pytest.approx(t * t * (3 - 2 * t))
    t = 4 / 9
    assert ps[-5] == pytest.approx(t * t * (3 - 2 * t))
    # Середина хода — середина пути, для каждого поля разом.
    half = stage_at(made, 66, fps, 1080, 1920)
    assert half["p"] == pytest.approx(0.5)
    assert half["width"] == pytest.approx((1080 + 560) / 2)
    assert half["scale"] == pytest.approx(0.8) and half["tx"] == pytest.approx(-10.0)


def test_stage_reads_the_scene_from_the_plan_dict_as_well():
    made = plan_scene()
    for frame in (55, 63, 100, 146, 151):
        assert stage_at(made.to_dict(), frame, 30, 1080, 1920) == stage_at(
            made, frame, 30, 1080, 1920
        )
    assert set(stage_at(made, 70, 30, 1080, 1920)) == {
        "p", "x", "y", "width", "height", "radius", "tilt", "scale", "tx", "ty",
    }


# ------------------------------------------------------------------ плашки


def test_a_chip_box_is_its_text_plus_padding_measured_by_the_font_metrics():
    """Коробка плашки сходится с независимым замером строки тем же шрифтом."""
    preset = load_preset("pulse").scaled(1920)
    made = measure_chip("одна истина", "pill", preset, 1920, fill="#F3D28F", color="#111111")
    look = made.look
    assert look["words"] == ["одна", "истина"] and look["style"] == "pill"
    ruler = preset.caption.pill.preset(preset)
    ruler.font.size = int(look["font_size"])
    ruler.caption.line_height = look["line_height"]
    text_w = measure_line(
        [(w, look["font_size"]) for w in look["words"]],
        metrics=metrics_for(ruler),
        letter_spacing=look["letter_spacing"],
        base_size=look["font_size"],
        word_spacing=look["word_spacing"],
    )
    assert made.width == pytest.approx(text_w + 2 * look["padding_x"])
    assert made.height == pytest.approx(
        line_box_height([look["font_size"]], ruler) + 2 * look["padding_y"]
    )
    # Капсула: скругление в половину высоты.
    assert look["radius"] == pytest.approx(made.height / 2, abs=0.01)


def test_a_chip_icon_widens_the_box_by_its_window_and_gap():
    preset = load_preset("pulse").scaled(1920)
    bare = measure_chip("одна истина", "pill", preset, 1920)
    icon = measure_chip("одна истина", "pill", preset, 1920, emoji="💡")
    assert bare.look["emoji"] is None and bare.look["emoji_box"] == 0
    assert icon.look["emoji"] == "💡" and icon.look["emoji_size"] > 0
    assert icon.width == pytest.approx(
        bare.width + icon.look["emoji_box"] + icon.look["emoji_gap"]
    )
    assert icon.height == pytest.approx(bare.height)
    # Слово вместо эмодзи значком не становится — окошко рассчитано на один глиф.
    assert measure_chip("одна истина", "pill", preset, 1920, emoji="ок").look["emoji"] is None


def test_chip_styles_look_the_way_the_sheet_asks():
    sheet = Visuals(
        overlays=[
            chip(20, "цена", style="pill"),
            chip(30, "цена", style="tag"),
            chip(40, "цена", style="label"),
        ]
    )
    plan = dress(talk(60), sheet)
    pill, tag, label = visuals_of(plan)["overlays"]
    accent = plan.preset["colors"]["accent"]
    # Капсула — акцент темы с контрастным текстом.
    assert pill["fill"] == accent and pill["rotate"] == 0
    assert overlays._contrast(pill["color"], pill["fill"]) > 4.5
    # Ценник — белый с тёмным текстом и наклоном в несколько градусов.
    assert tag["fill"] == "#FFFFFF" and luminance(tag["color"]) < 0.05
    assert 2 <= abs(tag["rotate"]) <= 6
    # Подпись — мелкая, в цвете акцента.
    assert label["fill"] == accent and label["font_size"] < 0.75 * pill["font_size"]
    for item in (pill, tag, label):
        assert item["kind"] == "chip" and item["font"] == "caption" and item["fill_opacity"] == 1


def test_the_chip_text_colour_follows_the_fill():
    assert overlays._ink_on("#FFE600") == overlays.INK_DARK
    assert overlays._ink_on("#1B2A4A") == overlays.INK_LIGHT


@pytest.mark.parametrize("name", ALL_PRESETS)
def test_the_ruble_sign_is_spelled_out_when_the_font_cannot_draw_it(name):
    preset = load_preset(name).scaled(1920)
    has_sign = "₽" in metrics_for(preset).advances
    made = measure_chip("9 500 000 ₽", "tag", preset, 1920)
    assert made is not None
    assert made.look["words"][:3] == ["9", "500", "000"]
    last = made.look["words"][-1]
    if has_sign:
        assert last == "₽"
    else:
        assert last.lower() == "руб." and "₽" not in "".join(made.look["words"])
        # И измерена именно подмена: коробка шире, чем была бы с одним знаком.
        shorter = measure_chip("9 500 000 р", "tag", preset, 1920)
        assert made.width > shorter.width


def test_the_ruble_fallback_is_decided_by_the_font_not_by_the_theme_name():
    assert [n for n in ALL_PRESETS if "₽" not in metrics_for(load_preset(n)).advances], (
        "в наборе не осталось шрифта без рубля — подмену стало не на чем проверять"
    )
    assert chip_text("5000₽", set("0123456789 руб.")) == "5000 руб."
    assert chip_text("5000 ₽", set("0123456789 ₽")) == "5000 ₽"
    assert chip_text("  −30 %  скидка ", set("−30% скида"), upper=True) == "−30 % СКИДКА"


def test_a_chip_shrinks_to_fit_and_gives_up_below_its_floor():
    preset = load_preset("pulse").scaled(1920)
    full = measure_chip("очень длинная плашка", "pill", preset, 1920)
    tight = measure_chip("очень длинная плашка", "pill", preset, 1920, max_width=full.width - 60)
    assert tight is not None and tight.width <= full.width - 60
    assert tight.look["font_size"] < full.look["font_size"]
    assert measure_chip("очень длинная плашка", "pill", preset, 1920, max_width=200) is None


def test_chips_scale_with_the_frame_height():
    big = measure_chip("цена", "pill", load_preset("pulse").scaled(1920), 1920)
    small = measure_chip("цена", "pill", load_preset("pulse").scaled(1280), 1280)
    assert small.look["font_size"] == pytest.approx(big.look["font_size"] * 1280 / 1920, abs=1)
    assert small.width == pytest.approx(big.width * 1280 / 1920, rel=0.04)


# ------------------------------------------------------------------ стикеры


@pytest.fixture
def sticker_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Свой набор: стикер с файлом, стикер-глиф и запись, чей файл пропал."""
    from PIL import Image

    folder = tmp_path / "stickers"
    folder.mkdir()
    Image.new("RGBA", (300, 150), (255, 0, 0, 255)).save(folder / "boat.png")
    Image.new("RGBA", (120, 240), (0, 255, 0, 255)).save(folder / "tower.png")
    index = {
        "version": 1,
        "stickers": {
            "boat": {"emoji": "⛵", "file": "boat.png", "aspect": 2.0},
            "tower": {"emoji": "🗼", "file": "tower.png", "aspect": None},
            "ghost": {"emoji": "👻", "file": None, "aspect": None},
            "lost": {"emoji": "🧭", "file": "lost.png", "aspect": 1.0},
        },
    }
    (folder / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(overlays, "STICKERS_DIR", folder)
    return folder


def sticker_plan(key: str | None = None, **kw) -> dict:
    sheet = Visuals(overlays=[Overlay(kind="sticker", word_idx=20, sticker=key, **kw)])
    (made,) = visuals_of(dress(talk(60), sheet, track=head(22)))["overlays"]
    return made


def test_a_sticker_with_a_file_is_a_picture_of_that_shape(sticker_set):
    made = sticker_plan("boat")
    assert made["kind"] == "sticker"
    assert Path(made["asset"]) == (sticker_set / "boat.png").resolve()
    assert Path(made["asset"]).is_absolute() and made["glyph_size"] == 0
    spot = rect_of(made)
    assert spot.width / spot.height == pytest.approx(2.0, abs=0.01)
    assert made["float_px"] > 0 and made["rotate"] != 0
    # Вход — по месту, куда стикер встал: сбоку, сверху или «pop» по центру.
    assert made["anim"] in ("pop", "from_left", "from_right", "drop")
    # Пропорция не записана в наборе — берётся из самого файла.
    spot = rect_of(sticker_plan("tower"))
    assert spot.width / spot.height == pytest.approx(0.5, abs=0.01)


def test_a_sticker_without_a_file_is_an_emoji_glyph(sticker_set):
    made = sticker_plan("ghost")
    assert made["asset"] is None and made["emoji"] == "👻"
    assert 0 < made["glyph_size"] <= 140
    spot = rect_of(made)
    # Место под глиф — по его картинке: она шире и выше кегля.
    assert spot.width >= made["glyph_size"] and spot.height >= made["glyph_size"]
    # Запись, чей файл пропал с диска, тоже рисуется глифом, а не дырой.
    lost = sticker_plan("lost")
    assert lost["asset"] is None and lost["emoji"] == "🧭" and lost["glyph_size"] > 0


def test_a_bare_emoji_is_a_glyph_sticker_and_an_unknown_key_is_nothing(sticker_set):
    made = sticker_plan(emoji="🔥")
    assert made["asset"] is None and made["emoji"] == "🔥" and 0 < made["glyph_size"] <= 140
    # Ключ неизвестен, но эмодзи есть — рисуется он.
    assert sticker_plan("нет-такого", emoji="🔥")["emoji"] == "🔥"
    sheet = Visuals(overlays=[Overlay(kind="sticker", word_idx=20, sticker="нет-такого")])
    assert visuals_of(dress(talk(60), sheet))["overlays"] == []


@pytest.mark.parametrize("height", [1280, 1920, 3840])
def test_the_emoji_glyph_never_grows_past_its_bitmap(height, sticker_set):
    """Цветной шрифт растровый: выше 140 px глиф мылится, каким бы ни был кадр."""
    sheet = Visuals(
        overlays=[Overlay(kind="sticker", word_idx=20, sticker="ghost")],
        scenes=[
            scene("card_top", 40, 48, items=[SceneItem(kind="sticker", word_idx=42, emoji="🔥")])
        ],
    )
    plan = dress(talk(60), sheet, width=height * 9 // 16, height=height)
    made = visuals_of(plan)
    sizes = [made["overlays"][0]["glyph_size"], made["scenes"][0]["items"][0]["glyph_size"]]
    assert all(0 < size <= 140 for size in sizes), sizes


def test_the_real_sticker_set_resolves_every_key():
    """Каждый ключ набора в репозитории чем-то рисуется: файлом или глифом."""
    index = json.loads((REPO / "assets" / "stickers" / "index.json").read_text(encoding="utf-8"))
    assert index["stickers"]
    for key, entry in index["stickers"].items():
        found = overlays._sticker(key, None)
        assert found is not None, key
        if entry.get("file") and (REPO / "assets" / "stickers" / entry["file"]).is_file():
            assert Path(found.asset).is_file() and found.aspect > 0, key
        else:
            assert found.asset is None and found.emoji, key


# ------------------------------------------------------------------ образец


def sample() -> tuple[EDL, dict]:
    raw = json.loads((REPO / "samples" / "edl-visuals.json").read_text(encoding="utf-8"))
    # Поле `visuals` у листа появляется отдельным швом: пока его нет, лишние
    # ключи срезаются, а графика передаётся явно.
    edl = EDL(**{k: v for k, v in raw.items() if k in EDL.model_fields})
    return edl, raw["visuals"]


def test_the_sample_sheet_gives_graphics_that_survive_json():
    edl, sheet = sample()
    plan = dressed_plan(
        build_plan, edl, "vertical.mp4", visuals=sheet, cfg=cfg(), fps=24, width=1080, height=1920
    )
    made = plan.meta["visuals"]
    assert made["version"] == 1
    assert [s["layout"] for s in made["scenes"]] == ["paper", "card_top"]
    assert {o["kind"] for o in made["overlays"]} >= {"chip", "sticker"}
    assert all(s["items"] for s in made["scenes"])
    # Туда-обратно через JSON — без потерь: это то, что уедет рендереру.
    assert json.loads(json.dumps(made, ensure_ascii=False)) == made
    again = RenderPlan.from_dict(json.loads(json.dumps(plan.to_dict(), ensure_ascii=False)))
    assert again.meta["visuals"] == made
    assert again.to_dict()["blocks"] == plan.to_dict()["blocks"]


def test_the_plan_speaks_the_contract_of_the_renderer():
    """Поля элемента и сцены — ровно те, что описаны в `render/src/visuals/types.ts`."""
    edl, sheet = sample()
    plan = dressed_plan(build_plan, edl, "vertical.mp4", visuals=sheet, cfg=cfg(), fps=24)
    made = plan.meta["visuals"]
    types = (REPO / "render" / "src" / "visuals" / "types.ts").read_text(encoding="utf-8")
    parts = [*made["overlays"], *(i for s in made["scenes"] for i in s["items"])]
    for item in parts:
        for key in item:
            assert f"  {key}:" in types, key
        assert item["kind"] in ("chip", "sticker", "decor", "photo", "map")
        assert item["layer"] == "front" and item["anim"] in (
            "pop", "slide", "fade", "from_left", "from_right", "drop", "stamp", "wipe"
        )
    for staged in made["scenes"]:
        for key in staged:
            assert f"  {key}:" in types, key
        assert staged["chrome"] in ("none", "phone", "polaroid")
        assert staged["bg"]["kind"] in ("paper", "board", "gradient", "still")


# ------------------------------------------------------------------ звук


def test_sound_accents_follow_what_survives():
    edl = talk(60)
    window_of = overlays._word_windows(edl, edl.timeline())
    sheet = Visuals(
        overlays=[
            chip(20, "плашка"),
            Overlay(kind="sticker", word_idx=30, emoji="🔥"),
            Overlay(kind="decor", word_idx=36, glyph="question"),
            chip(12, "в сцене"),  # выпадает: идёт сцена
            chip(59, "в конце"),  # выпадает: ролик кончается
        ],
        scenes=[scene("paper", 9, 17), scene("card_top", 3, 9)],  # вторая — слишком рано
    )
    events = sfx_events(sheet, window_of, edl.out_duration)
    assert events == sorted(events)
    assert [(round(t, 2), name) for t, name in events] == [
        (round(9 * STEP, 2), "whoosh"),
        (round(17 * STEP + 0.3, 2), "swish"),
        (round(20 * STEP, 2), "tick"),
        (round(30 * STEP, 2), "swish"),
        (round(36 * STEP, 2), "swish"),
    ]
    assert sfx_events(None, window_of, 21.0) == [] and sfx_events(Visuals(), window_of, 21.0) == []
    # Лист в виде словаря (сырой JSON) читается так же.
    assert sfx_events(sheet.model_dump(), window_of, edl.out_duration) == events


def test_sound_accents_know_what_the_plan_will_drop_when_given_the_edl():
    edl = talk(60)
    edl.broll = [Broll(word_idx=12, duration=1.5, query="q", anim="pop_in", anchor="card")]
    window_of = overlays._word_windows(edl, edl.timeline())
    sheet = Visuals(scenes=[scene("paper", 9, 17)], overlays=[chip(30, "плашка")])
    blind = sfx_events(sheet, window_of, edl.out_duration)
    aware = sfx_events(sheet, window_of, edl.out_duration, edl=edl, cfg=cfg())
    assert [name for _, name in blind] == ["whoosh", "swish", "tick"]
    # Сцена стоит на вставке — в плане её не будет, и звука у неё нет.
    assert [name for _, name in aware] == ["tick"]


def test_every_sound_accent_names_a_file_in_assets_sfx():
    names = {overlays.SFX_SCENE_IN, overlays.SFX_SCENE_OUT, *overlays.SFX_OVERLAY.values()}
    have = {p.stem for p in (REPO / "assets" / "sfx").iterdir() if p.suffix != ".md"}
    assert names <= have, names - have
    assert set(overlays.SFX_OVERLAY) == {"chip", "sticker", "decor"}


# ------------------------------------------------------------------ файлы рендера


def staged_plan(tmp_path: Path, sheet: Visuals, **kw) -> tuple[RenderPlan, Path]:
    plan = dress(talk(60), sheet, track=head(22), **kw)
    video = tmp_path / "job" / "video.mp4"
    video.parent.mkdir()
    video.write_bytes(b"x")
    plan.video = str(video)
    return plan, tmp_path / "job" / "assets"


def test_pictures_are_linked_into_the_job_folder_under_relative_names(tmp_path, photo, sticker_set):
    sheet = Visuals(
        overlays=[Overlay(kind="sticker", word_idx=30, sticker="boat")],
        scenes=[
            scene(
                "card_top",
                9,
                17,
                items=[
                    SceneItem(kind="photo", word_idx=11, asset=photo),
                    SceneItem(kind="sticker", word_idx=13, sticker="tower"),
                ],
            )
        ],
    )
    plan, assets = staged_plan(tmp_path, sheet)
    stage_visual_assets(plan, assets, cfg())
    made = plan.meta["visuals"]
    names = [made["overlays"][0]["asset"], *(i["asset"] for i in made["scenes"][0]["items"])]
    assert names == ["boat.png", "photo.png", "tower.png"]
    for name in names:
        assert (assets / name).is_file()
    assert (assets / "boat.png").read_bytes() == (sticker_set / "boat.png").read_bytes()
    # Ни одной картинки-глифа — шрифт эмодзи (10 МБ) в папку не едет.
    assert "emoji_font" not in plan.meta
    # Повторный вызов ничего не портит.
    before = copy.deepcopy(plan.meta)
    stage_visual_assets(plan, assets, cfg())
    assert plan.meta == before


def test_two_different_pictures_with_one_name_do_not_overwrite_each_other(tmp_path):
    from PIL import Image

    paths = []
    for n, colour in enumerate([(255, 0, 0), (0, 0, 255)]):
        folder = tmp_path / f"cache-{n}"
        folder.mkdir()
        Image.new("RGB", (40, 30), colour).save(folder / "pic.png")
        paths.append(str(folder / "pic.png"))
    sheet = Visuals(
        scenes=[
            scene(
                "card_top",
                9,
                17,
                items=[
                    SceneItem(kind="photo", word_idx=11 + n, asset=p) for n, p in enumerate(paths)
                ],
            )
        ]
    )
    plan, assets = staged_plan(tmp_path, sheet)
    stage_visual_assets(plan, assets, cfg())
    names = [i["asset"] for i in plan.meta["visuals"]["scenes"][0]["items"]]
    assert len(set(names)) == 2
    for name, source in zip(names, paths):
        assert (assets / name).read_bytes() == Path(source).read_bytes()


def test_a_missing_picture_never_breaks_the_reel(tmp_path, sticker_set):
    sheet = Visuals(
        overlays=[Overlay(kind="sticker", word_idx=30, sticker="boat")],
        scenes=[
            scene(
                "card_top",
                9,
                17,
                items=[
                    SceneItem(kind="photo", word_idx=11, asset=str(tmp_path / "нет.png")),
                    SceneItem(kind="chip", word_idx=13, text="жив"),
                ],
            )
        ],
    )
    plan, assets = staged_plan(tmp_path, sheet)
    (sticker_set / "boat.png").unlink()  # файл пропал между планом и рендером
    stage_visual_assets(plan, assets, cfg())
    made = plan.meta["visuals"]
    # Стикер рисуется глифом, фото выпадает, остальное на месте.
    sticker = made["overlays"][0]
    assert sticker["asset"] is None and sticker["emoji"] == "⛵" and 0 < sticker["glyph_size"] <= 140
    assert [i["kind"] for i in made["scenes"][0]["items"]] == ["chip"]
    assert plan.meta["emoji_font"] == "NotoColorEmoji.ttf"


@pytest.mark.parametrize(
    "overlay, wants_font",
    [
        (Overlay(kind="sticker", word_idx=30, emoji="🔥"), True),
        (Overlay(kind="chip", word_idx=30, text="цена", emoji="💰"), True),
        (Overlay(kind="chip", word_idx=30, text="цена"), False),
        (Overlay(kind="decor", word_idx=30, glyph="question"), False),
    ],
)
def test_the_emoji_font_rides_only_with_graphics_that_draw_a_glyph(tmp_path, overlay, wants_font):
    plan, assets = staged_plan(tmp_path, Visuals(overlays=[overlay]))
    stage_visual_assets(plan, assets, cfg())
    if wants_font:
        assert plan.meta["emoji_font"] == "NotoColorEmoji.ttf"
        assert (assets / "NotoColorEmoji.ttf").is_file()
    else:
        assert "emoji_font" not in plan.meta


def test_staging_a_plan_without_graphics_does_nothing(tmp_path):
    plan = build_plan(talk(30), "video.mp4", apply_cuts=False, cfg=cfg())
    before = plan.to_dict()
    stage_visual_assets(plan, tmp_path / "assets", cfg())
    assert plan.to_dict() == before and not (tmp_path / "assets").exists()


def test_the_phone_background_is_a_still_made_from_the_video(tmp_path, monkeypatch):
    calls = []

    def fake_still(video, at, out, frame):
        calls.append((Path(video).name, round(at, 2), frame))
        Path(out).write_bytes(b"jpg")
        return True

    monkeypatch.setattr(overlays, "_still", fake_still)
    plan, assets = staged_plan(tmp_path, Visuals(scenes=[scene("phone", 9, 17)]))
    staged = plan.meta["visuals"]["scenes"][0]
    assert staged["bg"]["kind"] == "still" and staged["bg"]["asset"] is None
    stage_visual_assets(plan, assets, cfg())
    assert staged["bg"]["asset"] == "scene-bg-0.jpg" and (assets / "scene-bg-0.jpg").is_file()
    # Один вызов, кадр из середины сцены, из видео плана.
    middle = round((staged["start"] + staged["end"]) / 2, 2)
    assert calls == [("video.mp4", middle, (1080, 1920))]
    # Второй раз кадр не снимается: он уже лежит в папке.
    stage_visual_assets(plan, assets, cfg())
    assert len(calls) == 1 and staged["bg"]["asset"] == "scene-bg-0.jpg"


def test_a_still_that_cannot_be_made_falls_back_to_a_plain_background(tmp_path, monkeypatch):
    monkeypatch.setattr(overlays, "_still", lambda *a, **k: False)
    plan, assets = staged_plan(tmp_path, Visuals(scenes=[scene("phone", 9, 17)]))
    stage_visual_assets(plan, assets, cfg())
    bg = plan.meta["visuals"]["scenes"][0]["bg"]
    assert bg["kind"] == "gradient" and bg["asset"] is None
    assert luminance(bg["color"]) < 0.05 and luminance(bg["color2"]) < 0.05


def test_the_still_is_a_blurred_darkened_frame_of_the_video(tmp_path):
    """Настоящий ffmpeg: кадр снят, он темнее исходного и в нём нет резких краёв."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("нужен ffmpeg в PATH")
    from PIL import Image, ImageFilter, ImageStat

    video = tmp_path / "video.mp4"
    made = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=360x640:rate=24:duration=2", "-pix_fmt", "yuv420p", str(video),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert made.returncode == 0, made.stderr[-1000:]
    out = tmp_path / "still.jpg"
    assert overlays._still(video, 1.0, out, (360, 640)) is True
    sharp = tmp_path / "sharp.png"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.0", "-i", str(video),
         "-frames:v", "1", str(sharp)],
        check=True, capture_output=True, timeout=120,
    )
    with Image.open(out) as still, Image.open(sharp) as source:
        assert still.size[0] / still.size[1] == pytest.approx(360 / 640, abs=0.01)
        dark = ImageStat.Stat(still.convert("L")).mean[0]
        light = ImageStat.Stat(source.convert("L")).mean[0]
        assert dark < 0.75 * light
        edges = ImageStat.Stat(still.convert("L").filter(ImageFilter.FIND_EDGES)).mean[0]
        crisp = ImageStat.Stat(
            source.convert("L").resize(still.size).filter(ImageFilter.FIND_EDGES)
        ).mean[0]
        assert edges < 0.3 * crisp
