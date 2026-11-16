"""Карточка со значком: стеклянная панель и круглый значок слева от неё.

Стекло под блоком уже было (`CaptionBlock.panel`, до двух блоков на ролик).
Значок — один эмодзи в круглом стеклянном окошке слева; панель и значок стоят в
кадре ВМЕСТЕ по центру, как одна группа. Решает режиссёр: значок получает только
блок со стеклом.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.core.config import Settings
from src.core.edl import EDL, CaptionBlock, CaptionWord, Emphasis, Word, validate_edl
from src.core.preset import load_preset
from src.core.render_plan import RenderPlan, build_plan
from src.pipeline.director import edl_from_plan

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "render"
FRAME = (1080, 1920)


def cfg() -> Settings:
    return Settings(_env_file=None, bot_token="x", sub_tail=0.15, render_fps=30,
                    render_width=1080, render_height=1920)


def words(texts: list[str]) -> list[Word]:
    return [Word(idx=i, text=t, start=i * 0.5, end=i * 0.5 + 0.4) for i, t in enumerate(texts)]


TEXTS = ["бота", "можно", "протестировать", "бесплатно", "и", "всё"]


def card_edl(emoji: str | None = "🎁", preset: str = "pulse", texts: list[str] | None = None) -> EDL:
    texts = texts or TEXTS
    last = len(texts) - 1
    return EDL(
        source_duration=len(texts) * 0.5,
        words=words(texts),
        cuts=[],
        preset=preset,
        captions=[
            CaptionBlock(
                words=[
                    CaptionWord(word_idx=i, emphasis=Emphasis.ACCENT if i == 2 else Emphasis.NONE)
                    for i in range(last - 1)
                ],
                position="center",
                panel=True,
                emoji=emoji,
            ),
            CaptionBlock(words=[CaptionWord(word_idx=last - 1), CaptionWord(word_idx=last)]),
        ],
    )


def plan(edl: EDL, **kw) -> RenderPlan:
    return build_plan(edl, "video.mp4", apply_cuts=False, cfg=cfg(), **kw)


# ------------------------------------------------------------------ режиссёр


def _raw(idxs: list[int], **kw) -> dict:
    base = {"words": idxs, "register": "poster", "accent_word": idxs[0], "accent": "accent",
            "position": "auto", "follow_face": False}
    base.update(kw)
    return base


def direct(blocks: list[dict]) -> EDL:
    n = max(i for b in blocks for i in b["words"]) + 1
    ws = [Word(idx=i, text=f"сл{i}", start=i * 0.4, end=i * 0.4 + 0.3) for i in range(n)]
    edl = edl_from_plan(ws, n * 0.4, {"hook_end_word": None, "drop_words": [], "blocks": blocks},
                        cfg=Settings(_env_file=None, bot_token="x"))
    assert validate_edl(edl) == []
    return edl


def test_only_a_glass_block_keeps_its_icon():
    edl = direct([
        _raw([0, 1], panel=True, emoji="🎁"),
        _raw([2, 3], emoji="🔥"),  # без стекла значку не на чем стоять
        _raw([4, 5], panel=True, emoji="подарок"),  # не эмодзи
    ])
    assert [b.emoji for b in edl.captions] == ["🎁", None, None]
    assert [b.panel for b in edl.captions] == [True, False, True]


def test_a_panel_trimmed_by_the_cap_loses_its_icon_too():
    # Карточки через обычный блок: подряд они не стоят, и считает их уже потолок.
    blocks = []
    for i in range(3):
        blocks += [_raw([2 * i], panel=True, emoji="🎁"), _raw([2 * i + 1])]
    edl = direct(blocks)
    assert [b.panel for b in edl.captions] == [True, False, True, False, False, False]
    assert [b.emoji for b in edl.captions] == ["🎁", None, "🎁", None, None, None]


# ------------------------------------------------------------------ план


def card_of(p: RenderPlan):
    return next(b for b in p.blocks if b.panel)


def test_the_icon_reaches_the_plan_with_its_numbers():
    block = card_of(plan(card_edl()))
    panel = load_preset("pulse").scaled(FRAME[1]).panel
    assert block.icon is not None
    assert block.icon.emoji == "🎁"
    assert 0 < block.icon.size <= panel.icon_size
    assert block.icon.gap == panel.icon_gap
    assert 0 < block.icon.emoji_size < block.icon.size


def test_the_panel_and_the_icon_stand_in_the_frame_as_one_group():
    from src.core.geometry import safe_margin_x

    block = card_of(plan(card_edl()))
    ps = load_preset("pulse").scaled(FRAME[1])
    room = block.icon.size + block.icon.gap
    # Группа стоит по центру кадра, а не панель по центру со значком сбоку.
    assert block.rect.x + block.rect.width / 2 == pytest.approx(FRAME[0] / 2)
    # Прямоугольник блока накрывает группу целиком: панель — это он без полосы
    # под значок, и она шире своей самой длинной строки ровно на поля и рамку.
    chrome = 2 * (ps.panel.padding_x + ps.panel.border_width)
    widest = max(_line_width(line, ps) for line in block.lines)
    assert block.rect.width - room == pytest.approx(widest + chrome, abs=0.01)
    # И вся группа — в безопасной зоне кадра.
    safe = safe_margin_x(ps, FRAME[0])
    assert block.rect.x >= safe - 0.01
    assert block.rect.x + block.rect.width <= FRAME[0] - safe + 0.01


def _line_width(line, ps) -> float:
    from src.core.geometry import SizedWord, measure_line, metrics_for

    return measure_line(
        [SizedWord(w.text, w.size) for w in line],
        metrics=metrics_for(ps),
        letter_spacing=ps.font.letter_spacing,
        base_size=float(ps.font.size),
        word_spacing=ps.font.word_spacing,
    )


def test_a_card_without_an_icon_keeps_the_full_line():
    """Полоса под значок отнимается у строки только там, где значок есть."""
    bare = card_of(plan(card_edl(emoji=None)))
    plain_edl = card_edl(emoji=None)
    plain_edl.captions[0].panel = False
    plain = plan(plain_edl).blocks[0]
    assert bare.icon is None
    assert bare.rect == plain.rect and bare.lines == plain.lines


def test_the_icon_is_never_taller_than_its_panel():
    block = card_of(plan(card_edl()))
    assert block.icon.size <= block.rect.height


def test_a_theme_without_glass_has_no_icon():
    name = next(n for n in ("glass", "bold", "feather") if not load_preset(n).panel.enabled)
    block = plan(card_edl(preset=name)).blocks[0]
    assert block.icon is None


def test_a_group_wider_than_the_safe_frame_drops_the_icon():
    """Значок — украшение: если с ним группа не влезает, блок остаётся без него.

    Так бывает со словом, которое не ужать до ширины строки: оно сознательно
    переполняет бюджет (пол кегля), и сдвигать его ещё и значком незачем.
    """
    giant = "сверхдлинноеслово" * 4
    block = card_of(plan(card_edl(texts=["бота", "можно", giant, "бесплатно", "и", "всё"])))
    assert block.icon is None


def test_a_block_without_glass_never_shows_an_icon():
    edl = card_edl()
    edl.captions[0].panel = False
    assert all(b.icon is None for b in plan(edl).blocks)


def test_icons_survive_the_json_round_trip():
    p = plan(card_edl())
    again = RenderPlan.from_dict(p.to_dict())
    assert [b.icon for b in again.blocks] == [b.icon for b in p.blocks]
    data = p.to_dict()
    for b in data["blocks"]:
        b.pop("icon")
    assert all(b.icon is None for b in RenderPlan.from_dict(data).blocks)


def test_the_emoji_font_rides_with_a_card_icon(tmp_path):
    from src.pipeline.render import prepare_assets

    p = plan(card_edl())
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    p.video = str(video)
    prepare_assets(p, tmp_path / "assets", cfg=cfg())
    assert p.meta["emoji_font"] == "NotoColorEmoji.ttf"


# ------------------------------------------------------------------ рендер


def test_the_renderer_draws_the_icon_from_the_plan():
    captions = (RENDER / "src" / "layers" / "Captions.tsx").read_text()
    types = (RENDER / "src" / "types.ts").read_text()
    block = re.search(r"icon\?: \{(.*?)\} \| null;", types, re.S)
    assert block, "в types.ts нет PlanBlock.icon"
    for field in ("emoji", "size", "emoji_size", "gap"):
        assert f"{field}:" in block.group(1), field
    # Значок стоит абсолютно и на коробку панели не влияет: её мерил Python.
    assert "badge.size" in captions and "badge.gap" in captions and "badge.emoji_size" in captions
    assert 'right: "100%"' in captions


def test_the_director_is_told_about_the_icon():
    from src.llm.schemas import DIRECT_SCHEMA

    text = DIRECT_SCHEMA["properties"]["blocks"]["items"]["properties"]["emoji"]["description"]
    assert "panel" in text
    prompt = (REPO / "src" / "llm" / "prompts" / "direct.md").read_text()
    assert "значок" in prompt.lower()
    json.dumps(DIRECT_SCHEMA)  # схема остаётся сериализуемой


# ------------------------------------------------------------------ чередование приёмов
#
# Особые приёмы — карточка (блок со стеклом) и список плашками — работают, пока
# стоят среди обычных блоков. Два подряд читаются как сбой стиля, поэтому соседа
# мы разжалуем сами: список остаётся, карточка рядом с ним становится плакатом.


def _item(idxs: list[int]) -> dict:
    return {"words": idxs, "register": "item", "accent_word": None, "accent": None,
            "position": "auto", "follow_face": False, "emoji": "🎵"}


def test_two_cards_in_a_row_leave_one():
    edl = direct([
        _raw([0, 1], panel=True, emoji="🎁"),
        _raw([2, 3], panel=True, emoji="💰"),
        _raw([4, 5]),
        _raw([6, 7], panel=True, emoji="⏱️"),
    ])
    assert [b.panel for b in edl.captions] == [True, False, False, True]
    assert [b.emoji for b in edl.captions] == ["🎁", None, None, "⏱️"]


def test_a_card_next_to_a_list_gives_way_to_it():
    edl = direct([
        _raw([0, 1], panel=True, emoji="🎁"),  # прямо перед списком
        _item([2]), _item([3]),
        _raw([4, 5], panel=True, emoji="💰"),  # прямо после списка
        _raw([6, 7]),
        _raw([8, 9], panel=True, emoji="⏱️"),  # отделена обычным блоком
    ])
    assert [b.item for b in edl.captions] == [False, True, True, False, False, False]
    assert [b.panel for b in edl.captions] == [False, False, False, False, False, True]


def test_the_cap_counts_only_the_cards_that_survived():
    """Потолок в две карточки считает уцелевшие, а не заявленные."""
    edl = direct([
        _raw([0], panel=True), _raw([1], panel=True),  # вторая — сосед первой
        _raw([2]),
        _raw([3], panel=True),
        _raw([4]),
        _raw([5], panel=True),  # третья уцелевшая — сверх потолка
    ])
    assert [b.panel for b in edl.captions] == [True, False, False, True, False, False]


def test_the_director_is_told_how_to_mix_the_tricks():
    prompt = (REPO / "src" / "llm" / "prompts" / "direct.md").read_text()
    assert "## Как чередовать приёмы" in prompt
    section = prompt.split("## Как чередовать приёмы")[1].split("\n## ")[0]
    for word in ("подряд", "item", "panel"):
        assert word in section.lower(), word
