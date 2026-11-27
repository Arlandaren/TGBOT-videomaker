"""Редактор сцен: лист → сцены и правки → новый лист (`core/edit.py`).

Всё здесь — данные: ни базы, ни файлов. Сборку версии ролика из нового листа
проверяет прогон на сервере.
"""

from __future__ import annotations

import pytest

from src.core.edit import EditError, apply_edits, changed, scenes_of
from src.core.edl import Broll, BrollAnim, CaptionBlock, CaptionWord, Cut, Emphasis, Sfx
from tests.test_edl import base_edl, blocks, words


def edl9(**kw):
    """Девять слов по полсекунды, три сцены по три слова."""
    return base_edl(**kw)


def test_scenes_follow_the_caption_blocks():
    scenes = scenes_of(edl9(), apply_cuts=True)
    assert [s["i"] for s in scenes] == [0, 1, 2]
    assert scenes[0]["text"] == "w0 w1 w2"
    assert [w["idx"] for w in scenes[1]["words"]] == [3, 4, 5]
    assert scenes[0]["start"] == 0.0 and scenes[1]["start"] == pytest.approx(1.5)
    assert scenes[0]["position"] == "auto" and scenes[0]["size"] == "m" and scenes[0]["media"] is None


def test_a_scene_cut_away_by_the_edit_is_not_a_scene():
    """Вторая сцена целиком под вырезанным куском — в ролике её нет."""
    cuts = [Cut(src_in=0.0, src_out=1.45, reason="keep"), Cut(src_in=1.45, src_out=3.0, reason="drop_silence"),
            Cut(src_in=3.0, src_out=4.9, reason="keep")]
    edl = edl9(cuts=cuts)
    scenes = scenes_of(edl, apply_cuts=True)
    assert [s["i"] for s in scenes] == [0, 2]
    # Времена — уже на таймлайне результата: третья сцена идёт сразу за первой.
    assert scenes[1]["start"] == pytest.approx(1.45)
    # Ролик собран без нарезки — куски не применены, и сцена на месте.
    assert [s["i"] for s in scenes_of(edl, apply_cuts=False)] == [0, 1, 2]


def test_text_with_the_same_number_of_words_replaces_them_one_to_one():
    edl = edl9()
    new, cut_on = apply_edits(edl, {"scenes": {"1": {"text": "Эта тачка лям"}}}, apply_cuts=True)
    assert [new.words[i].text for i in (3, 4, 5)] == ["Эта", "тачка", "лям"]
    assert cut_on is True and changed(edl, new)
    # Исходный лист не тронут, времена слов те же.
    assert edl.words[3].text == "w3"
    assert [(w.start, w.end) for w in new.words] == [(w.start, w.end) for w in edl.words]


def test_extra_words_ride_in_the_last_word():
    new, _ = apply_edits(edl9(), {"scenes": {"0": {"text": "Нет брат вот Курей лям 300"}}}, apply_cuts=True)
    assert [new.words[i].text for i in (0, 1, 2)] == ["Нет", "брат", "вот Курей лям 300"]
    assert [cw.word_idx for cw in new.captions[0].words] == [0, 1, 2]


def test_fewer_words_span_the_whole_scene_and_keep_the_accent_slot():
    """Два слова на три места: первое и последнее, чтобы текст стоял всю сцену."""
    edl = edl9(captions=blocks([[0, 1, 2], [3, 4, 5], [6, 7, 8]], {2: Emphasis.ACCENT}))
    new, _ = apply_edits(edl, {"scenes": {"0": {"text": "Привет мир"}}}, apply_cuts=True)
    block = new.captions[0]
    assert [cw.word_idx for cw in block.words] == [0, 2]
    assert [new.words[0].text, new.words[2].text] == ["Привет", "мир"]
    assert block.words[1].emphasis == Emphasis.ACCENT
    # Слово 1 в кадре не показывается, но из ролика не вырезано.
    assert new.cuts == edl.cuts and new.words[1].text == "w1"


def test_empty_text_removes_the_scene_but_not_the_speech():
    edl = edl9()
    new, _ = apply_edits(edl, {"scenes": {"1": {"text": "   "}}}, apply_cuts=True)
    assert len(new.captions) == 2
    assert [cw.word_idx for b in new.captions for cw in b.words] == [0, 1, 2, 6, 7, 8]
    assert new.cuts == edl.cuts


def test_cutting_a_word_removes_it_from_the_frame_and_from_the_video():
    cuts = [Cut(src_in=0.0, src_out=4.9, reason="hook", framing="tight")]
    edl = edl9(cuts=cuts, sfx=[Sfx(at_word_idx=4, name="whoosh")])
    new, cut_on = apply_edits(edl, {"scenes": {"1": {"cut": [4]}}}, apply_cuts=True)
    assert cut_on
    assert [cw.word_idx for cw in new.captions[1].words] == [3, 5]
    kept = [(c.src_in, c.src_out) for c in new.kept_cuts]
    # Слово 4 звучит 2.0–2.4, следующее начинается в 2.5: пауза уходит вместе со словом.
    assert kept == [(0.0, 2.0), (2.5, 4.9)]
    assert new.kept_cuts[0].reason == "hook" and new.kept_cuts[1].reason == "keep"
    assert all(c.framing == "tight" for c in new.kept_cuts)
    assert new.sfx == [], "звук на вырезанном слове не играет"
    # Сцены нового листа: слово пропало, время сцены сжалось.
    scene = scenes_of(new, apply_cuts=True)[1]
    assert scene["text"] == "w3 w5"


def test_cutting_a_word_in_an_uncut_reel_cuts_only_that_word():
    """Ролик собран «без нарезки»: вырезанное слово включает нарезку, но паузы остаются."""
    cuts = [Cut(src_in=0.0, src_out=1.0, reason="keep"), Cut(src_in=1.0, src_out=1.5, reason="drop_silence"),
            Cut(src_in=1.5, src_out=4.9, reason="keep")]
    edl = edl9(cuts=cuts)
    new, cut_on = apply_edits(edl, {"scenes": {"2": {"cut": [7]}}}, apply_cuts=False)
    assert cut_on is True
    assert [(c.src_in, c.src_out) for c in new.kept_cuts] == [(0.0, 3.5), (4.0, 4.9)]


def test_neighbouring_cut_words_make_one_hole():
    edl = edl9(cuts=[Cut(src_in=0.0, src_out=4.9, reason="keep")])
    new, _ = apply_edits(edl, {"scenes": {"1": {"cut": [3, 4]}}}, apply_cuts=True)
    assert [(c.src_in, c.src_out) for c in new.kept_cuts] == [(0.0, 1.5), (2.5, 4.9)]


def test_cutting_everything_is_refused():
    edl = base_edl(words=words(2), groups=[[0, 1]], cuts=[Cut(src_in=0.0, src_out=0.9, reason="keep")],
                   source_duration=0.9)
    with pytest.raises(EditError) as err:
        apply_edits(edl, {"scenes": {"0": {"cut": [0, 1]}}}, apply_cuts=True)
    assert err.value.code == "edit_empty"


def test_only_the_scenes_own_words_can_be_cut():
    with pytest.raises(EditError):
        apply_edits(edl9(), {"scenes": {"0": {"cut": [5]}}}, apply_cuts=True)


def test_position_and_size_are_written_to_the_block():
    new, _ = apply_edits(edl9(), {"scenes": {"0": {"position": "top", "size": "l"}}}, apply_cuts=True)
    assert new.captions[0].position == "top" and new.captions[0].size == "l"
    assert new.captions[1].size == "m"
    with pytest.raises(EditError):
        apply_edits(edl9(), {"scenes": {"0": {"size": "huge"}}}, apply_cuts=True)


def test_a_quiet_block_moved_down_becomes_a_poster():
    """Тихий регистр бывает только наверху: блок, поставленный человеком ниже,
    играет плакатом — иначе рендерер получил бы блок, какого не умеет."""
    quiet = CaptionBlock(words=[CaptionWord(word_idx=i) for i in (0, 1, 2)], position="top", quiet=True)
    edl = edl9(captions=[quiet, *blocks([[3, 4, 5], [6, 7, 8]])])
    new, _ = apply_edits(edl, {"scenes": {"0": {"position": "bottom"}}}, apply_cuts=True)
    assert new.captions[0].position == "bottom" and new.captions[0].quiet is False


def test_a_long_quiet_block_stays_at_the_top():
    ws = words(9)
    long_quiet = CaptionBlock(words=[CaptionWord(word_idx=i) for i in range(6)], position="top", quiet=True)
    edl = base_edl(words=ws, captions=[long_quiet, *blocks([[6, 7, 8]])])
    assert scenes_of(edl, apply_cuts=True)[0]["movable"] is False
    new, _ = apply_edits(edl, {"scenes": {"0": {"position": "center"}}}, apply_cuts=True)
    assert new.captions[0].position == "top" and new.captions[0].quiet is True


def test_a_single_list_item_plays_as_a_poster():
    items = [CaptionBlock(words=[CaptionWord(word_idx=i)], item=True, emoji="✅") for i in (0, 1)]
    edl = base_edl(words=words(5), captions=[*items, *blocks([[2, 3, 4]])])
    new, _ = apply_edits(edl, {"scenes": {"1": {"text": ""}}}, apply_cuts=True)
    assert new.captions[0].item is False and new.captions[0].emoji is None


def test_an_own_picture_is_attached_to_the_scene_and_replaces_the_old_one():
    old = Broll(word_idx=4, duration=2.0, query="car", anim=BrollAnim.POP_IN, anchor="card", asset="/cache/car.png")
    edl = edl9(broll=[old])
    scene = scenes_of(edl, apply_cuts=True)[1]
    assert scene["media"] == {"word": "w4", "custom": False}

    new, _ = apply_edits(edl, {"scenes": {"1": {"media": {"op": "set", "key": "edit-media/42/a.jpg"}}}},
                         apply_cuts=True)
    assert len(new.broll) == 1
    pic = new.broll[0]
    assert pic.word_idx == 3 and pic.asset_key == "edit-media/42/a.jpg" and pic.asset is None
    assert 1.5 <= pic.duration <= 2.5
    assert scenes_of(new, apply_cuts=True)[1]["media"] == {"word": "w3", "custom": True}

    gone, _ = apply_edits(edl, {"scenes": {"1": {"media": {"op": "remove"}}}}, apply_cuts=True)
    assert gone.broll == []


def test_pictures_too_close_are_refused_with_a_clear_code():
    edits = {"scenes": {"0": {"media": {"op": "set", "key": "k1"}}, "1": {"media": {"op": "set", "key": "k2"}}}}
    with pytest.raises(EditError) as err:
        apply_edits(edl9(), edits, apply_cuts=True)
    assert err.value.code == "edit_media_close"


def test_a_picture_on_a_cut_word_goes_with_it():
    old = Broll(word_idx=4, duration=2.0, query="car", anim=BrollAnim.POP_IN, anchor="card")
    edl = edl9(broll=[old], cuts=[Cut(src_in=0.0, src_out=4.9, reason="keep")])
    new, _ = apply_edits(edl, {"scenes": {"1": {"cut": [4]}}}, apply_cuts=True)
    assert new.broll == []


def test_the_title_can_be_rewritten_or_removed():
    edl = edl9(title="Старый заголовок")
    new, _ = apply_edits(edl, {"title": "  Новый   заголовок "}, apply_cuts=True)
    assert new.title == "Новый заголовок"
    none, _ = apply_edits(edl, {"title": ""}, apply_cuts=True)
    assert none.title is None


def test_no_edits_change_nothing():
    edl = edl9()
    new, cut_on = apply_edits(edl, {"scenes": {}}, apply_cuts=False)
    assert not changed(edl, new) and cut_on is False


@pytest.mark.parametrize("bad", [{"scenes": {"9": {}}}, {"scenes": {"x": {}}}, {"scenes": []},
                                 {"scenes": {"0": {"text": "я" * 400}}}, {"scenes": {"0": {"position": "left"}}},
                                 {"scenes": {"0": {"media": {"op": "set"}}}}])
def test_garbage_is_refused(bad):
    with pytest.raises(EditError):
        apply_edits(edl9(), bad, apply_cuts=True)


def test_the_edited_list_still_builds_a_plan():
    """Новый лист обязан собираться в план рендера: вырезанное слово, своя
    картинка, размер и место — всё разом."""
    from src.core.render_plan import build_plan

    edl = edl9(cuts=[Cut(src_in=0.0, src_out=4.9, reason="keep")])
    edits = {"scenes": {"0": {"text": "Привет мир", "size": "l", "position": "top"},
                        "1": {"cut": [4]}, "2": {"media": {"op": "set", "key": "k"}, "size": "s"}}}
    new, cut_on = apply_edits(edl, edits, apply_cuts=True)
    plan = build_plan(new, "video.mp4", fps=30, width=1080, height=1920, apply_cuts=cut_on)
    assert len(plan.blocks) == 3 and len(plan.broll) == 1
    texts = [" ".join(w.text for w in b.words) for b in plan.blocks]
    assert "w4" not in " ".join(texts)

    def biggest(block):
        return max(w.size or 0 for w in block.words)

    base = build_plan(edl, "video.mp4", fps=30, width=1080, height=1920, apply_cuts=True)
    assert biggest(plan.blocks[2]) < biggest(base.blocks[2]), "маленький размер — мельче кегля темы"


def test_cutting_a_word_in_a_reel_without_any_cuts():
    """В листе нет ни одного реза (ролик без пауз): вырезанное слово режется из
    цельного исходника, а не объявляет ролик пустым."""
    edl = edl9()
    assert edl.cuts == []
    new, cut_on = apply_edits(edl, {"scenes": {"1": {"cut": [4]}}}, apply_cuts=True)
    assert cut_on and [(c.src_in, c.src_out) for c in new.kept_cuts] == [(0.0, 2.0), (2.5, edl.source_duration)]
