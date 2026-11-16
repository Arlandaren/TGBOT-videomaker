"""Перечисление: пункты отдельными плашками с эмодзи.

Когда человек перечисляет («трендовые звуки, вставленные картинки, …»), каждый
пункт встаёт своей плашкой под предыдущим и остаётся до конца списка. Решает
режиссёр — блоком `register: "item"`; подряд идущие пункты и есть список.

Перебор с приёмом — не ошибка формы: одиночный пункт, слишком длинный пункт или
третий список за ролик молча становятся обычными блоками, ролик от этого не
падает и модель по кругу не гоняется (так же устроены плашка и привязка к голове).
"""

from __future__ import annotations

from src.core.config import Settings
from src.core.edl import (
    LIST_ITEM_MAX_WORDS,
    LIST_MAX,
    LIST_MAX_ITEMS,
    CaptionBlock,
    CaptionWord,
    Emphasis,
    Word,
    list_runs,
    validate_edl,
)
from src.pipeline.director import edl_from_plan


def cfg(**kw) -> Settings:
    return Settings(_env_file=None, bot_token="x", **kw)


def words(n: int) -> list[Word]:
    return [Word(idx=i, text=f"сл{i}", start=i * 0.4, end=i * 0.4 + 0.3) for i in range(n)]


def item(idxs: list[int], emoji: str | None = "🎵") -> dict:
    return {"words": idxs, "register": "item", "accent_word": None, "accent": None,
            "position": "auto", "follow_face": False, "emoji": emoji}


def poster(idxs: list[int]) -> dict:
    return {"words": idxs, "register": "poster", "accent_word": idxs[0], "accent": "accent",
            "position": "auto", "follow_face": False}


def plan_of(blocks: list[dict]) -> dict:
    return {"hook_end_word": None, "drop_words": [], "blocks": blocks}


def build(blocks: list[dict]):
    n = max(i for b in blocks for i in b["words"]) + 1
    edl = edl_from_plan(words(n), n * 0.4, plan_of(blocks), cfg=cfg())
    assert validate_edl(edl) == []
    return edl


# ------------------------------------------------------------------ режиссёр


def test_consecutive_items_make_a_list():
    edl = build([poster([0, 1]), item([2, 3], "🎵"), item([4, 5], "🖼️"), poster([6, 7])])
    assert [b.item for b in edl.captions] == [False, True, True, False]
    assert [b.emoji for b in edl.captions] == [None, "🎵", "🖼️", None]
    assert list_runs(edl.captions) == [[1, 2]]


def test_an_item_carries_no_gold_no_panel_and_does_not_follow_the_face():
    raw = item([2, 3])
    raw.update({"accent_word": 2, "accent": "shout", "follow_face": True, "panel": True})
    edl = build([poster([0, 1]), raw, item([4, 5])])
    block = edl.captions[1]
    assert block.item is True
    assert all(w.emphasis == Emphasis.NONE for w in block.words)
    assert block.anchor_track is None and block.panel is False and block.quiet is False


def test_a_lonely_item_becomes_an_ordinary_block():
    """Список из одного пункта — не список: плашка посреди плакатов читается как сбой."""
    edl = build([poster([0, 1]), item([2, 3]), poster([4, 5])])
    block = edl.captions[1]
    assert block.item is False and block.emoji is None
    # Обычному плакату нужно главное слово — как любому блоку без заявки (С3).
    assert sum(w.emphasis != Emphasis.NONE for w in block.words) == 1


def test_a_long_item_breaks_the_list_not_the_job():
    long = list(range(2, 2 + LIST_ITEM_MAX_WORDS + 1))
    last = long[-1]
    edl = build([item([0, 1]), item(long), item([last + 1, last + 2]), item([last + 3])])
    # Длинный пункт — обычный блок; он рвёт цепочку, слева остался один пункт
    # (тоже разжалован), справа — два, это список.
    assert [b.item for b in edl.captions] == [False, False, True, True]


def test_a_list_holds_at_most_four_items():
    blocks = [item([i]) for i in range(LIST_MAX_ITEMS + 2)]
    edl = build(blocks)
    flags = [b.item for b in edl.captions]
    assert flags[:LIST_MAX_ITEMS] == [True] * LIST_MAX_ITEMS
    assert flags[LIST_MAX_ITEMS:] == [False, False]


def test_a_reel_holds_at_most_two_lists():
    blocks: list[dict] = []
    i = 0
    for _ in range(LIST_MAX + 1):
        blocks += [item([i]), item([i + 1]), poster([i + 2])]
        i += 3
    edl = build(blocks)
    assert len(list_runs(edl.captions)) == LIST_MAX
    # Первые остаются, лишний разжалован — как у плашки и привязки к голове.
    assert [b.item for b in edl.captions[-3:]] == [False, False, False]


def test_only_a_single_emoji_survives():
    edl = build([item([0], "🎵🎵"), item([1], "звук"), item([2], " 🖼️ "), item([3], None)])
    assert [b.emoji for b in edl.captions] == [None, None, "🖼️", None]
    assert all(b.item for b in edl.captions)


def test_a_plan_without_items_is_untouched():
    edl = build([poster([0, 1]), poster([2, 3])])
    assert [b.item for b in edl.captions] == [False, False]
    assert list_runs(edl.captions) == []


# ------------------------------------------------------------------ контракт EDL


def _edl_with(blocks: list[CaptionBlock]):
    from src.core.edl import EDL

    n = max(w.word_idx for b in blocks for w in b.words) + 1
    return EDL(source_duration=n * 0.4, words=words(n), cuts=[], captions=blocks, preset="pulse")


def _block(idxs: list[int], **kw) -> CaptionBlock:
    return CaptionBlock(words=[CaptionWord(word_idx=i) for i in idxs], **kw)


def test_a_hand_written_edl_is_held_to_the_same_rules():
    lonely = _edl_with([_block([0], item=True), _block([1])])
    assert any("пункт" in e for e in validate_edl(lonely, strict_coverage=False))

    long = _edl_with([
        _block(list(range(LIST_ITEM_MAX_WORDS + 1)), item=True),
        _block([LIST_ITEM_MAX_WORDS + 1], item=True),
    ])
    assert any("пункт" in e for e in validate_edl(long, strict_coverage=False))

    gold = _edl_with([
        CaptionBlock(words=[CaptionWord(word_idx=0, emphasis=Emphasis.ACCENT)], item=True),
        _block([1], item=True),
    ])
    assert any("пункт" in e for e in validate_edl(gold, strict_coverage=False))

    fine = _edl_with([_block([0], item=True, emoji="🎵"), _block([1], item=True)])
    assert [e for e in validate_edl(fine, strict_coverage=False) if "пункт" in e] == []


def test_an_edl_saved_before_lists_reads_as_before():
    block = CaptionBlock.model_validate({"words": [{"word_idx": 0}], "position": "auto"})
    assert block.item is False and block.emoji is None


def test_the_schema_offers_the_item_register_and_the_emoji():
    from src.llm.schemas import DIRECT_SCHEMA

    block = DIRECT_SCHEMA["properties"]["blocks"]["items"]
    assert "item" in block["properties"]["register"]["enum"]
    assert block["properties"]["emoji"]["type"] == ["string", "null"]
    assert "emoji" not in block["required"]


# ------------------------------------------------------------------ план рендера
#
# Пункты списка — стопка плашек: общий левый край, каждый появляется на своём
# первом слове и стоит до конца списка. Раскладку решает Python, рендерер рисует
# готовые прямоугольники.

import pytest

from src.core.edl import EDL
from src.core.preset import load_preset
from src.core.render_plan import RenderPlan, build_plan
from src.core.track import FaceTrack, TrackPoint

FRAME = (1080, 1920)
TEXTS = ["бот", "добавляет", "трендовые", "звуки", "вставленные", "картинки",
         "живые", "субтитры", "и", "всё"]


def plan_cfg() -> Settings:
    return Settings(_env_file=None, bot_token="x", sub_tail=0.15, render_fps=30,
                    render_width=1080, render_height=1920)


def list_edl(captions: list[CaptionBlock] | None = None, texts: list[str] | None = None) -> EDL:
    texts = texts or TEXTS
    ws = [Word(idx=i, text=t, start=i * 0.5, end=i * 0.5 + 0.4) for i, t in enumerate(texts)]
    captions = captions or [
        _block([0, 1]),
        _block([2, 3], item=True, emoji="🎵"),
        _block([4, 5], item=True, emoji="🖼️"),
        _block([6, 7], item=True, emoji="💬"),
        _block([8, 9]),
    ]
    return EDL(source_duration=len(texts) * 0.5, words=ws, cuts=[], captions=captions,
               preset="pulse")


def plan_of_edl(edl: EDL, **kw) -> RenderPlan:
    return build_plan(edl, "video.mp4", apply_cuts=False, cfg=plan_cfg(), **kw)


def pills(plan: RenderPlan):
    return [b for b in plan.blocks if b.pill is not None]


def test_a_list_becomes_a_stack_of_pills():
    plan = plan_of_edl(list_edl())
    stack = pills(plan)
    assert len(stack) == 3
    assert [b.pill.emoji for b in stack] == ["🎵", "🖼️", "💬"]
    assert [" ".join(w.text for w in b.words) for b in stack] == [
        "трендовые звуки", "вставленные картинки", "живые субтитры",
    ]
    for b in stack:
        assert b.rect is not None and len(b.lines) == 1
        assert all(w.emphasis == "none" for w in b.words)
        assert not b.quiet and not b.title_card and b.anchor_path is None


def test_pills_share_the_left_edge_and_stack_downwards():
    plan = plan_of_edl(list_edl())
    stack = pills(plan)
    gap = load_preset("pulse").scaled(FRAME[1]).caption.pill.gap
    assert len({round(b.rect.x, 3) for b in stack}) == 1
    for upper, lower in zip(stack, stack[1:]):
        assert lower.rect.y == pytest.approx(upper.rect.y + upper.rect.height + gap)
    for b in stack:
        assert b.rect.x >= 0 and b.rect.x + b.rect.width <= FRAME[0]
        assert b.rect.y >= 0 and b.rect.y + b.rect.height <= FRAME[1]


def test_a_pill_enters_on_its_first_word_and_stays_until_the_list_ends():
    plan = plan_of_edl(list_edl())
    stack = pills(plan)
    assert [b.start for b in stack] == [1.0, 2.0, 3.0]
    # Список уходит целиком — когда начинается следующий блок.
    after = next(b for b in plan.blocks if b.pill is None and b.start >= 4.0)
    assert {b.end for b in stack} == {after.start}
    # И блок перед списком не лежит под первой плашкой.
    before = next(b for b in plan.blocks if b.pill is None and b.start < 1.0)
    assert before.end <= stack[0].start


def test_the_whole_text_of_a_pill_is_there_from_its_first_frame():
    """Плашка — название пункта, а не караоке: текст виден сразу и до конца."""
    plan = plan_of_edl(list_edl())
    for b in pills(plan):
        assert {w.start for w in b.words} == {b.start}


def test_a_pill_is_as_wide_as_its_text_plus_the_icon():
    plan = plan_of_edl(list_edl())
    with_icon = pills(plan)[0]
    edl = list_edl()
    edl.captions[1].emoji = None
    bare = pills(plan_of_edl(edl))[0]
    assert bare.pill.emoji is None
    extra = with_icon.pill.emoji_box + with_icon.pill.emoji_gap
    assert with_icon.rect.width == pytest.approx(bare.rect.width + extra)
    assert with_icon.rect.height == pytest.approx(bare.rect.height)


def test_the_stack_stays_off_the_face():
    """Лицо в нижней половине кадра — стопка встаёт туда, где глаз и рта нет."""
    fps = 30
    low_face = FaceTrack(fps=fps, points=[
        TrackPoint(f=f, cx=0.5, cy=0.68, h=0.22, conf=0.9) for f in range(0, 6 * fps)
    ])
    plan = plan_of_edl(list_edl(), track=low_face)
    stack = pills(plan)
    assert len(stack) == 3
    top = min(b.rect.y for b in stack)
    bottom = max(b.rect.y + b.rect.height for b in stack)
    face_top = (0.68 - 0.25 * 0.22) * FRAME[1]
    face_low = (0.68 + 0.5 * 0.22) * FRAME[1]
    assert bottom <= face_top or top >= face_low


def test_a_list_that_does_not_fit_plays_as_ordinary_blocks():
    long = ["бот"] + ["оченьдлинноеслововпунктесписка"] * 8 + ["всё"]
    edl = list_edl(
        captions=[
            _block([0]),
            _block([1, 2, 3, 4], item=True, emoji="🎵"),
            _block([5, 6, 7, 8], item=True, emoji="🖼️"),
            _block([9]),
        ],
        texts=long,
    )
    plan = plan_of_edl(edl)
    assert pills(plan) == []
    # Ни одно слово не пропало: пункты просто сыграли обычными блоками.
    shown = [w.word_idx for b in plan.blocks for w in b.words]
    assert shown == list(range(10))


def test_a_theme_can_switch_pills_off():
    preset = load_preset("pulse").model_copy(deep=True)
    preset.caption.pill.enabled = False
    plan = plan_of_edl(list_edl(), preset=preset)
    assert pills(plan) == []
    assert len(plan.blocks) == 5


def test_pills_survive_the_json_round_trip():
    plan = plan_of_edl(list_edl())
    again = RenderPlan.from_dict(plan.to_dict())
    assert [b.pill for b in again.blocks] == [b.pill for b in plan.blocks]
    assert again.to_dict() == plan.to_dict()


def test_a_plan_saved_before_pills_still_reads():
    data = plan_of_edl(list_edl()).to_dict()
    for b in data["blocks"]:
        b.pop("pill")
    assert all(b.pill is None for b in RenderPlan.from_dict(data).blocks)


def test_a_picture_gives_way_to_the_stack():
    """Картинка разводится с плашками так же, как с любым текстом (DoD Ф7)."""
    from src.core.edl import Broll

    edl = list_edl()
    edl.broll = [Broll(word_idx=3, duration=2.0, query="q", anim="pop_in", anchor="center")]
    plan = plan_of_edl(edl)
    picture = plan.broll[0]
    assert picture.rect is not None
    for b in pills(plan):
        if b.start < picture.end and b.end > picture.start:
            assert not picture.rect.overlaps(b.rect), (picture.rect, b.rect)


def test_every_theme_lays_the_stack_inside_the_frame():
    from src.core.preset import available

    for name in available():
        edl = list_edl()
        edl.preset = name
        plan = plan_of_edl(edl)
        stack = pills(plan)
        assert len(stack) == 3, name
        for b in stack:
            assert b.rect.x >= 0 and b.rect.x + b.rect.width <= FRAME[0], name


# ------------------------------------------------------------------ рендер
#
# Значок рисует цветной эмодзи-шрифт. На сервере рендера его в системе нет
# вовсе, поэтому шрифт лежит в репозитории и едет в бандл вместе с роликом.

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "render"


def test_the_emoji_font_is_in_the_repository():
    font = plan_cfg().emoji_font
    assert font.exists() and font.stat().st_size > 1_000_000
    # Не среди шрифтов текста: у тех выгружены метрики и каждый сверяется с Chromium.
    assert font.parent != plan_cfg().fonts_dir


def test_the_emoji_font_rides_with_a_plan_that_has_icons(tmp_path):
    from src.pipeline.render import RenderRequest, _request_payload, prepare_assets

    plan = plan_of_edl(list_edl())
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    plan.video = str(video)
    assets = tmp_path / "assets"
    prepare_assets(plan, assets, cfg=plan_cfg())
    assert plan.meta["emoji_font"] == "NotoColorEmoji.ttf"
    assert (assets / "NotoColorEmoji.ttf").exists()
    payload = json.loads(_request_payload(
        RenderRequest(plan=plan, out=tmp_path / "o.mp4", assets_dir=assets, job_key="j")
    ))
    assert payload["plan"]["emojiFontUrl"] == "NotoColorEmoji.ttf"


def test_a_plan_without_icons_does_not_carry_ten_megabytes(tmp_path):
    from src.pipeline.render import RenderRequest, _request_payload, prepare_assets

    edl = list_edl()
    for block in edl.captions:
        block.emoji = None
    plan = plan_of_edl(edl)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    plan.video = str(video)
    prepare_assets(plan, tmp_path / "assets", cfg=plan_cfg())
    assert "emoji_font" not in plan.meta
    payload = json.loads(_request_payload(
        RenderRequest(plan=plan, out=tmp_path / "o.mp4", assets_dir=tmp_path / "assets", job_key="j")
    ))
    assert payload["plan"]["emojiFontUrl"] is None


def test_the_renderer_draws_the_pill_the_plan_describes():
    """Текстовый контракт: рендерер берёт числа плашки из плана, а не выводит свои."""
    captions = (RENDER / "src" / "layers" / "Captions.tsx").read_text()
    root = (RENDER / "src" / "Root.tsx").read_text()
    types = (RENDER / "src" / "types.ts").read_text()
    cli = (RENDER / "render.mjs").read_text()

    assert re.search(r'new FontFace\(\s*"ReelbotEmoji",\s*`url\(\$\{staticFile\(emojiFontUrl\)\}\)`', root)
    assert '"ReelbotEmoji"' in captions
    assert "emojiFontUrl: withPrefix(prefix, plan.emojiFontUrl)" in cli
    # Кегль, разрядка и окошко значка — из блока; место — из его прямоугольника.
    for field in ("font_size", "letter_spacing", "word_spacing", "emoji_box", "emoji_size", "emoji_gap"):
        assert f"{field}: number" in types, field
        assert f"pill.{field}" in captions or f"icon.{field}" in captions, field
    assert "spot.x" in captions and "spot.y" in captions
    # Числа темы для плашки рендерер знает те же, что Python.
    from src.core.preset import Pill

    declared = set(re.search(r"pill\?: \{\s*enabled: boolean;(.*?)\};", types, re.S).group(1).split())
    for field in ("line_height", "padding_x", "padding_y", "radius", "blur", "border_width",
                  "border_color", "border_opacity", "tint_color", "tint_opacity",
                  "glass_color", "glass_opacity"):
        assert field in Pill.model_fields
        assert f"{field}:" in declared, field


@pytest.mark.slow
@pytest.mark.parametrize("name", ["pulse", "deco_line", "notebook", "bold", "cursive", "silk"])
def test_chromium_draws_the_pill_the_plan_promised(name: str):
    """Коробка плашки в плане и в настоящем Chromium — в пределах 2 px.

    Тот же довод, что у обычного блока: Python решает, где стоит стопка и что с
    ней не пересекается, а рисует браузер. Окошко значка в замер входит — это
    добавка к левому полю, и ошибиться можно именно в ней.
    """
    measure = RENDER / "measure.mjs"
    if shutil.which("node") is None or not (RENDER / "node_modules").exists():
        pytest.skip("нужен node и render/node_modules")

    edl = list_edl()
    edl.preset = name
    edl.captions[2].emoji = None  # одна плашка без значка
    plan = plan_of_edl(edl)
    stack = pills(plan)
    assert len(stack) == 3
    base = load_preset(name).scaled(FRAME[1])
    look = stack[0].pill
    pr = base.caption.pill.preset(base, look.font_size / base.caption.pill.font_size)
    assert pr.font.size == look.font_size

    request = {
        "font": str(plan_cfg().fonts_dir / pr.font.file),
        "weight": pr.font.weight,
        "base_size": pr.font.size,
        "letter_spacing": pr.font.letter_spacing,
        "word_spacing": pr.font.word_spacing,
        "line_height": pr.caption.line_height,
        "items": [],
        "boxes": [
            {
                "id": f"p{i}",
                "lines": [[{"text": w.text, "size": w.size} for w in b.lines[0]]],
                "padding_x": pr.panel.padding_x,
                "padding_y": pr.panel.padding_y,
                "border_width": pr.panel.border_width,
                "icon_room": b.pill.emoji_box + b.pill.emoji_gap,
                "icon_box": b.pill.emoji_box,
                "icon_size": b.pill.emoji_size,
            }
            for i, b in enumerate(stack)
        ],
    }
    out = subprocess.run(
        ["node", str(measure)], input=json.dumps(request, ensure_ascii=False),
        capture_output=True, text=True, timeout=600, cwd=str(RENDER),
    )
    assert out.returncode == 0, out.stderr[-2000:]
    measured = json.loads(out.stdout)["boxes"]
    for i, b in enumerate(stack):
        assert measured[f"p{i}"]["width"] == pytest.approx(b.rect.width, abs=2.0), (name, i)
        assert measured[f"p{i}"]["height"] == pytest.approx(b.rect.height, abs=2.0), (name, i)


def test_the_ass_backend_still_writes_a_list_without_falling_over():
    """Запасной рендер плашек не рисует, но и ролик из-за них не теряет."""
    from src.pipeline.subs_ass import build_ass

    text = build_ass(plan_of_edl(list_edl()))
    for word in ("трендовые", "картинки", "субтитры"):
        assert word in text
