"""Расстановщик графики (`pipeline/visuals.py`) и правила её листа (`core/overlay_rules.py`).

Главное требование одно на оба модуля: что бы ни вернула модель и что бы ни
лежало в листе, дальше по конвейеру уезжает лист без единого нарушения — и ни
один платный вызов не делается там, где его не просили.

Сети здесь нет: модель подменена двойником (`tests/conftest.py`), всё остальное
— арифметика над листом.
"""

from __future__ import annotations

import copy
import json
import random
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from src.core import overlay_rules
from src.core.config import Settings
from src.core.edl import EDL, Cut, Word
from src.core.overlay_models import (
    CHIP_TEXT_MAX,
    OVERLAY_MAX_VISIBLE,
    OVERLAY_MIN_GAP,
    SCENE_ITEMS_MAX,
    SCENE_MIN_GAP,
    ChipStyle,
    DecorGlyph,
    Overlay,
    OverlayKind,
    Scene,
    SceneItem,
    SceneItemKind,
    SceneLayout,
    Slot,
    Visuals,
    overlay_window,
    scene_window,
)
from src.core.overlay_rules import (
    check_visuals,
    digit_groups,
    overlay_cap,
    sanitize_visuals,
    scene_cap,
    sticker_index,
    sticker_keys,
    validate_visuals,
    window_of_edl,
)
from src.llm import jsonshape
from src.llm.client import LLMClient, load_prompt, render
from src.llm.providers import ProviderError
from src.pipeline import visuals as planner
from src.pipeline.visuals import (
    ADJ_ENDINGS,
    LEXICON_PATH,
    NOUN_ENDINGS,
    VISUALS_PROMPT,
    VISUALS_SCHEMA,
    VISUALS_TIER,
    decor_marks,
    fallback_visuals,
    lexicon_stickers,
    load_lexicon,
    normalize_word,
    number_chips,
    plan_visuals,
    prompt_variables,
    timed_words_table,
    visuals_from_answer,
)
from tests.conftest import FakeProvider, result_of

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "visuals"


@pytest.fixture(autouse=True)
def _fixed_list_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Хвост списка плашек правила берут из настроек — закрепляем его.

    Тест проверяет правила, а не `.env` конкретной машины: с другим `SUB_TAIL`
    окно списка сдвинулось бы и поменяло, какая сцена с ним пересекается.
    """
    monkeypatch.setattr(overlay_rules.default_settings, "sub_tail", 0.6)


# ------------------------------------------------------------------ листы монтажа


def edl_of(text: str, *, step: float = 0.5, length: float = 0.4, **extra: Any) -> EDL:
    """Лист монтажа из фразы: слово каждые `step` секунд, без резов."""
    tokens = text.split()
    words = [
        Word(idx=i, text=t, start=round(i * step, 3), end=round(i * step + length, 3))
        for i, t in enumerate(tokens)
    ]
    total = round(len(tokens) * step, 3)
    return EDL(
        source_duration=total,
        words=words,
        cuts=[Cut(src_in=0.0, src_out=total, reason="keep")],
        captions=[],
        preset="pulse",
        **extra,
    )


def load_edl(raw: dict[str, Any]) -> EDL:
    """Лист из JSON, без ключей, которых модель листа не знает."""
    return EDL.model_validate({k: v for k, v in raw.items() if k in EDL.model_fields})


def story(**patch: Any) -> EDL:
    """Общий лист тестов: 57 слов, 28 с, вырезанное «Эээ» (слово 5), список, вставка.

    Слово `i` начинается на `i·0.5` с до реза и на `i·0.5 − 0.5` после него.
    Числа стоят на словах 10–12 (9 500 000), 16 (3), 21 (11,5), 26 (21), 36 (18);
    список плашками — слова 31–33, вставка-картинка — слово 44.
    """
    raw = json.loads((FIXTURES / "edl.json").read_text(encoding="utf-8"))
    raw.update(patch)
    return load_edl(raw)


def chip(word: int, text: str, **kw: Any) -> Overlay:
    return Overlay(kind="chip", word_idx=word, text=text, **kw)


def sticker(word: int, key: str = "house", **kw: Any) -> Overlay:
    return Overlay(kind="sticker", word_idx=word, sticker=key, **kw)


def decor(word: int, glyph: str = "sparkle", **kw: Any) -> Overlay:
    return Overlay(kind="decor", word_idx=word, glyph=glyph, **kw)


def sheet(*overlays: Overlay, scenes: list[Scene] | None = None) -> Visuals:
    return Visuals(overlays=list(overlays), scenes=scenes or [])


def texts(visuals: Visuals) -> list[str | None]:
    return [o.text for o in visuals.overlays if o.kind == "chip"]


# ------------------------------------------------------------------ окна слов


def test_word_window_is_on_the_output_timeline_and_none_for_cut_words():
    edl = story()
    window_of = window_of_edl(edl)
    assert window_of(4) == pytest.approx((2.0, 2.4))
    assert window_of(5) is None, "«Эээ» вырезано монтажом"
    assert window_of(6) == pytest.approx((2.5, 2.9)), "после реза время сдвинуто на вырезанное"
    assert window_of(999) is None and window_of(-1) is None


def test_caps_follow_the_duration():
    assert [overlay_cap(d) for d in (0.0, 3.9, 4.1, 18.47, 28.0, 300.0)] == [0, 1, 2, 5, 7, 8]
    assert [scene_cap(d) for d in (0.0, 10.0, 15.1, 300.0)] == [0, 1, 2, 2]
    assert scene_cap(60.0, split=True) == 0


# ------------------------------------------------------------------ проверка: каждое правило


def scene(layout: str, first: int, last: int, **kw: Any) -> Scene:
    return Scene(layout=layout, word_from=first, word_to=last, **kw)


# (что в листе, обрывок сообщения, которое обязан увидеть режиссёр)
RULES: list[tuple[str, Visuals, str]] = [
    ("chip_without_text", sheet(Overlay(kind="chip", word_idx=7)), "нужен text"),
    ("sticker_key_not_in_set", sheet(sticker(7, "unicorn")), "нет в наборе"),
    ("sticker_without_anything", sheet(Overlay(kind="sticker", word_idx=7)), "нужен sticker"),
    (
        "sticker_with_two_emoji",
        sheet(Overlay(kind="sticker", word_idx=7, emoji="🔥🔥")),
        "ровно ОДИН эмодзи",
    ),
    ("decor_without_glyph", sheet(Overlay(kind="decor", word_idx=7)), "нужен glyph"),
    ("word_does_not_exist", sheet(sticker(999)), "отсутствует в words"),
    ("word_is_cut", sheet(sticker(5)), "вырезано"),
    ("until_before_start", sheet(sticker(10, until_word_idx=8)), "не позже word_idx"),
    ("until_on_a_cut_word", sheet(sticker(2, until_word_idx=5)), "вырезано"),
    ("invented_number", sheet(chip(21, "12 млн")), "не звучит в речи"),
    ("number_only_far_away", sheet(chip(30, "21%")), "не звучит в речи"),
    (
        "over_the_cap",
        sheet(*(sticker(w) for w in (0, 6, 14, 20, 24, 28, 34, 40))),
        "при потолке 7",
    ),
    ("entries_too_close", sheet(sticker(10), decor(11)), "нужно не меньше 1.5"),
    (
        "too_many_at_once",
        sheet(
            chip(10, "раз", until_word_idx=30),
            chip(13, "два", until_word_idx=30),
            chip(16, "три", until_word_idx=30),
        ),
        "одновременно 3",
    ),
    ("no_time_left", sheet(chip(56, "конец")), "до конца ролика"),
    (
        "overlay_during_a_scene",
        sheet(sticker(8), scenes=[scene("phone", 6, 13)]),
        "на время сцены",
    ),
    ("scene_too_short", sheet(scenes=[scene("phone", 16, 17)]), "короче 2.5"),
    ("scene_too_long", sheet(scenes=[scene("phone", 6, 30)]), "дольше 6"),
    ("scene_in_the_hook", sheet(scenes=[scene("phone", 1, 8)]), "в первые 2.5"),
    ("scene_in_the_tail", sheet(scenes=[scene("phone", 50, 56)]), "до конца ролика"),
    ("scene_reversed", sheet(scenes=[scene("phone", 13, 6)]), "раньше word_from"),
    ("scene_on_a_missing_word", sheet(scenes=[scene("phone", 6, 999)]), "отсутствует в words"),
    (
        "scenes_over_the_cap",
        sheet(scenes=[scene("phone", 6, 13), scene("phone", 36, 42), scene("phone", 49, 54)]),
        "при потолке 2",
    ),
    (
        "scenes_too_close",
        sheet(scenes=[scene("phone", 36, 42), scene("phone", 49, 54)]),
        "между сценами",
    ),
    (
        "scenes_overlap",
        sheet(scenes=[scene("phone", 6, 13), scene("paper", 8, 14)]),
        "пересекаются",
    ),
    ("scene_over_the_list", sheet(scenes=[scene("card_top", 29, 36)]), "со списком"),
    ("scene_over_the_broll", sheet(scenes=[scene("paper", 41, 47)]), "со вставкой"),
    (
        "item_outside_the_scene",
        sheet(
            scenes=[scene("phone", 6, 13, items=[SceneItem(kind="chip", word_idx=20, text="а")])]
        ),
        "вне сцены",
    ),
    (
        "item_chip_without_text",
        sheet(scenes=[scene("phone", 6, 13, items=[SceneItem(kind="chip", word_idx=7)])]),
        "нужен text",
    ),
    (
        "item_with_invented_number",
        sheet(
            scenes=[scene("phone", 6, 13, items=[SceneItem(kind="chip", word_idx=7, text="7 млн")])]
        ),
        "не звучит в речи",
    ),
    (
        "heading_with_invented_number",
        sheet(scenes=[scene("phone", 6, 13, heading="Топ 5 квартир")]),
        "из заголовка",
    ),
]


@pytest.mark.parametrize("name,visuals,needle", RULES, ids=[r[0] for r in RULES])
def test_every_rule_is_reported_in_words_and_fixed_by_sanitize(name, visuals, needle):
    edl = story()
    errors = validate_visuals(edl, visuals)
    assert any(needle in e for e in errors), f"{name}: нет сообщения про «{needle}»: {errors}"
    clean = sanitize_visuals(visuals, edl)
    assert validate_visuals(edl, clean) == []
    assert sanitize_visuals(clean, edl) == clean


def test_no_scene_at_all_when_the_frame_is_split():
    edl = story(split_until=6.0)
    visuals = sheet(chip(26, "21%"), scenes=[scene("phone", 6, 13), scene("phone", 36, 42)])
    errors = validate_visuals(edl, visuals)
    assert len(errors) == 1 and errors[0].startswith("scenes: кадр поделён на два видео")
    clean = sanitize_visuals(visuals, edl)
    assert clean.scenes == [] and texts(clean) == ["21%"]


def test_no_free_overlay_while_the_frame_is_split():
    """Сборка плана выбрасывает элемент, вошедший до конца деления, — и правила тоже.

    Условие то же, что в `overlays._live_overlays`: начало раньше `split_until`.
    Разойдись они, лист проходил бы проверку и терял элемент уже в плане.
    """
    edl = story(split_until=6.0)
    # Слово 12 входит на 5.5 с — до конца деления; слово 13 — ровно на 6.0 с.
    early, on_time = chip(12, "рано"), sticker(13, "banknote")
    errors = validate_visuals(edl, sheet(early, on_time))
    assert len(errors) == 1 and "overlays[0]" in errors[0] and "поделён на два видео" in errors[0]
    clean = sanitize_visuals(sheet(early, on_time), edl)
    assert [o.kind for o in clean.overlays] == ["sticker"]
    assert validate_visuals(story(), sheet(early)) == [], "без деления кадра элемент годится"


def test_form_errors_of_a_raw_sheet_are_reported_not_raised():
    """Лист из ручной правки JSON: форму ловит модель, а сообщение — наше."""
    edl = story()
    raw = {"overlays": [{"kind": "chip", "word_idx": 7, "text": "х" * (CHIP_TEXT_MAX + 1)}]}
    errors = validate_visuals(edl, raw)
    assert errors and f"длиннее {CHIP_TEXT_MAX} знаков" in errors[0]
    assert "overlays[0].text" in errors[0]
    assert validate_visuals(edl, {"overlays": [{"kind": "hologram", "word_idx": 1}]})
    assert validate_visuals(edl, ["не", "лист"])


def test_a_sheet_built_around_the_model_is_still_checked():
    """`model_construct` обходит проверку формы — правила обязаны её вернуть."""
    edl = story()
    rogue = Visuals.model_construct(
        version=1,
        overlays=[Overlay.model_construct(kind="chip", word_idx=7, text="х" * 60)],
        scenes=[],
    )
    assert validate_visuals(edl, rogue)
    assert sanitize_visuals(rogue, edl) == Visuals()


def test_nothing_to_check_is_an_empty_list():
    edl = story()
    assert validate_visuals(edl) == [], "у листа монтажа нет графики — проверять нечего"
    assert validate_visuals(edl, Visuals()) == []
    assert sanitize_visuals(None, edl) == Visuals()


def test_visuals_default_to_the_sheet_inside_the_edl():
    """`validate_visuals(edl)` берёт графику из самого листа монтажа."""

    class WithVisuals:
        """Лист монтажа с полем `visuals` — каким он станет после интеграции."""

        def __init__(self, edl: EDL, visuals: Visuals) -> None:
            self._edl, self.visuals = edl, visuals

        def __getattr__(self, name: str) -> Any:
            return getattr(self._edl, name)

    edl = WithVisuals(story(), sheet(chip(21, "12 млн")))
    assert any("не звучит в речи" in e for e in validate_visuals(edl))  # type: ignore[arg-type]


def test_validation_messages_tell_excess_from_mistakes():
    """Перебор срезается молча, ошибка — повод для ретрая: их нельзя путать."""
    edl = story()
    crowded = check_visuals(edl, sheet(sticker(10), decor(11)))
    assert crowded and all(issue.excess for issue in crowded)
    lied = check_visuals(edl, sheet(chip(21, "12 млн"), sticker(999)))
    assert len(lied) == 2 and not any(issue.excess for issue in lied)


# ------------------------------------------------------------------ правило честности


@pytest.mark.parametrize(
    "text,groups",
    [
        ("от 9 500 000 ₽", ["9500000"]),
        ("9\u00a0500\u00a0000", ["9500000"]),  # неразрывные пробелы
        ("9500000 и 9.500.000", ["9500000", "9500000"]),
        ("11,5 млн и 11.5", ["115", "115"]),
        ("в 3 раза за 5 лет", ["3", "5"]),
        ("2024 500", ["2024", "500"]),  # год тройкой тысяч не продолжается
        ("1 2345", ["1", "2345"]),
        ("+21% и $300", ["21", "300"]),
        ("без цифр", []),
    ],
)
def test_digit_groups_ignore_spaces_and_separators(text, groups):
    assert digit_groups(text) == groups


@pytest.mark.parametrize(
    "text,honest",
    [
        ("9 500 000 ₽", True),
        ("9500000 ₽", True),
        ("9,5 млн ₽", False),  # другие цифры: в речи «9 500 000»
        ("500 000 ₽", False),  # обрывок сказанного числа
        ("9 500 ₽", False),
        ("10 млн", False),
        ("дорого", True),
    ],
)
def test_a_chip_number_must_be_the_spoken_number(text, honest):
    edl = story()
    errors = validate_visuals(edl, sheet(chip(10, text)))
    assert (errors == []) is honest, errors


def test_a_number_said_only_in_a_cut_word_is_not_heard():
    edl = edl_of("цена была 500 рублей но это неважно совсем уже давно")
    assert validate_visuals(edl, sheet(chip(1, "500 ₽"))) == []
    cut = edl.model_copy(
        update={
            "cuts": [
                Cut(src_in=0.0, src_out=1.0, reason="keep"),
                Cut(src_in=1.0, src_out=1.5, reason="drop_filler"),  # слово 2 — «500»
                Cut(src_in=1.5, src_out=5.0, reason="keep"),
            ]
        }
    )
    assert window_of_edl(cut)(2) is None
    assert any("не звучит в речи" in e for e in validate_visuals(cut, sheet(chip(1, "500 ₽"))))


def test_a_far_end_word_does_not_widen_where_the_number_may_sound():
    """«Рядом» — пока элемент в кадре, а не до слова, названного концом."""
    edl = story()
    # Плашка на слове 2 живёт максимум 4 с — до слова 9; «21» звучит на слове 26.
    assert any(
        "не звучит в речи" in e
        for e in validate_visuals(edl, sheet(chip(2, "21%", until_word_idx=27)))
    )


# ------------------------------------------------------------------ чистка


def test_sanitize_returns_a_new_sheet_and_leaves_the_input_alone():
    edl = story()
    visuals = sheet(chip(10, "  9 500 000 ₽ ", until_word_idx=3), sticker(999))
    before = visuals.model_dump()
    clean = sanitize_visuals(visuals, edl)
    assert visuals.model_dump() == before
    assert clean is not visuals and clean.overlays[0] is not visuals.overlays[0]
    assert clean.overlays[0].text == "9 500 000 ₽" and clean.overlays[0].until_word_idx is None


def test_numbers_win_over_stickers_and_stickers_over_decor():
    edl = story()
    # Все три входят в одну секунду — место достаётся одному.
    clean = sanitize_visuals(sheet(decor(9), sticker(11), chip(10, "9 500 000 ₽")), edl)
    assert [o.kind for o in clean.overlays] == ["chip"]
    clean = sanitize_visuals(sheet(decor(9), sticker(11)), edl)
    assert [o.kind for o in clean.overlays] == ["sticker"]
    # Плашка со словом ценнее стикера, но уступает плашке с числом.
    clean = sanitize_visuals(sheet(chip(9, "купили"), sticker(11), chip(10, "9 500 000 ₽")), edl)
    assert texts(clean) == ["9 500 000 ₽"]


def test_among_equals_the_earliest_stay_and_the_result_is_in_time_order():
    edl = story()
    crowd = sheet(*(sticker(w) for w in (52, 46, 40, 34, 28, 24, 20, 14, 6, 0)))
    clean = sanitize_visuals(crowd, edl)
    assert [o.word_idx for o in clean.overlays] == [0, 6, 14, 20, 24, 28, 34]
    assert len(clean.overlays) == overlay_cap(edl.timeline().duration) == 7


def test_python_owned_fields_survive_sanitize():
    """Файл картинки, место и зерно ставит Python — чистка их не трогает."""
    edl = story()
    visuals = sheet(sticker(10, asset="stickers/house.png", slot="top_left", seed=5))
    kept = sanitize_visuals(visuals, edl).overlays[0]
    assert (kept.asset, kept.slot, kept.seed) == ("stickers/house.png", "top_left", 5)


def test_density_rules_hold_in_seconds_after_sanitize():
    """Чистка считает теми же окнами, что и сборка плана (`overlay_window`)."""
    edl = story()
    window_of = window_of_edl(edl)
    duration = edl.timeline().duration
    crowd = sheet(*(chip(w, "факт", until_word_idx=min(56, w + 12)) for w in range(0, 56, 2)))
    clean = sanitize_visuals(crowd, edl)
    windows = [overlay_window(o, window_of, duration) for o in clean.overlays]
    assert len(windows) >= 4 and all(w is not None for w in windows)
    starts = [w[0] for w in windows]
    assert all(b - a >= OVERLAY_MIN_GAP - 1e-6 for a, b in zip(starts, starts[1:]))
    for moment in [w[0] + 0.01 for w in windows]:
        assert sum(1 for a, b in windows if a <= moment < b) <= OVERLAY_MAX_VISIBLE


# ------------------------------------------------------------------ враждебные листы


def hostile_files() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json") if p.name != "edl.json")


def test_there_are_hostile_fixtures():
    assert len(hostile_files()) >= 8


@pytest.mark.parametrize("path", hostile_files(), ids=lambda p: p.stem)
def test_hostile_sheet_is_cleaned_to_a_valid_one_and_stays_put(path: Path):
    case = json.loads(path.read_text(encoding="utf-8"))
    edl = story(**case.get("edl", {}))
    raw = copy.deepcopy(case["visuals"])

    clean = sanitize_visuals(case["visuals"], edl)
    assert case["visuals"] == raw, "вход чистки не меняется"
    assert validate_visuals(edl, clean) == [], "после чистки нарушений нет"
    assert sanitize_visuals(clean, edl) == clean, "повторная чистка ничего не меняет"
    # Лист переживает сохранение: то же самое из JSON и обратно.
    assert sanitize_visuals(json.loads(clean.model_dump_json()), edl) == clean

    expect = case["expect"]
    assert len(clean.overlays) == expect["overlays"], [o.model_dump() for o in clean.overlays]
    assert len(clean.scenes) == expect["scenes"]
    shown = texts(clean)
    for text in expect.get("texts", []):
        assert text in shown, f"плашка «{text}» должна была остаться: {shown}"
    for text in expect.get("absent_texts", []):
        assert text not in shown, f"плашка «{text}» должна была выпасть"
    for key in expect.get("stickers", []):
        assert key in [o.sticker for o in clean.overlays]
    for emoji in expect.get("emojis", []):
        assert emoji in [o.emoji for o in clean.overlays]
    for glyph in expect.get("glyphs", []):
        assert glyph in [o.glyph for o in clean.overlays]
    if expect.get("until_all_null"):
        assert all(o.until_word_idx is None for o in clean.overlays)
    if "headings" in expect:
        assert [s.heading for s in clean.scenes] == expect["headings"]
    if "layouts" in expect:
        assert [s.layout for s in clean.scenes] == expect["layouts"]
    if "scene_items" in expect:
        assert [len(s.items) for s in clean.scenes] == expect["scene_items"]


def _random_sheet(rng: random.Random) -> dict[str, Any]:
    """Случайный лист: годное вперемешку с негодным. Зерно задаёт вызывающий."""
    keys = sorted(sticker_keys())
    pool = [
        "9 500 000 ₽", "500 000 ₽", "3 года", "11,5 млн", "12 млн", "21%", "+21%", "18%",
        "дорого", "", "   ", "считай деньги", "х" * 40, "№1", None,
    ]

    def word() -> Any:
        return rng.choice([rng.randint(-3, 60), rng.randint(0, 56), rng.randint(0, 56), "x", None])

    def overlay() -> Any:
        if rng.random() < 0.05:
            return rng.choice(["мусор", 7, None, []])
        item: dict[str, Any] = {
            "kind": rng.choice(["chip", "chip", "sticker", "sticker", "decor", "banner"]),
            "word_idx": word(),
        }
        if rng.random() < 0.4:
            item["until_word_idx"] = word()
        if rng.random() < 0.8:
            item["text"] = rng.choice(pool)
        if rng.random() < 0.5:
            item["sticker"] = rng.choice([*keys[:20], "unicorn", None])
        if rng.random() < 0.3:
            item["emoji"] = rng.choice(["🔥", "🔥🔥", "5️⃣", "слово", "", None])
        if rng.random() < 0.5:
            item["glyph"] = rng.choice([*get_args(DecorGlyph), "heart", None])
        if rng.random() < 0.2:
            item["slot"] = rng.choice([*get_args(Slot), "moon"])
        if rng.random() < 0.1:
            item["extra"] = 1
        return item

    def one_scene() -> Any:
        first = rng.randint(-2, 58)
        return {
            "layout": rng.choice([*get_args(SceneLayout), "hologram"]),
            "word_from": first,
            "word_to": first + rng.randint(-2, 16),
            "heading": rng.choice([None, "Ипотека", "Топ 5", "18% годовых", "х" * 50]),
            "items": [
                {
                    "kind": rng.choice([*get_args(SceneItemKind), "video"]),
                    "word_idx": first + rng.randint(-2, 10),
                    "text": rng.choice(pool),
                    "sticker": rng.choice([*keys[:10], "unicorn", None]),
                }
                for _ in range(rng.randint(0, 5))
            ],
        }

    return {
        "overlays": [overlay() for _ in range(rng.randint(0, 40))],
        "scenes": [one_scene() for _ in range(rng.randint(0, 6))],
    }


@pytest.mark.parametrize("seed", range(12))
def test_any_sheet_is_cleaned_to_a_valid_one(seed: int):
    """Свойство, а не пример: чистка обязана справиться с любым листом.

    Листы случайные, но зерно явное — прогон воспроизводим до элемента.
    """
    rng = random.Random(seed)
    for edl in (story(), story(split_until=6.0), story(cuts=[], broll=[])):
        duration = edl.timeline().duration
        for _ in range(25):
            raw = _random_sheet(rng)
            clean = sanitize_visuals(raw, edl)
            assert validate_visuals(edl, clean) == [], raw
            assert sanitize_visuals(clean, edl) == clean, raw
            assert len(clean.overlays) <= overlay_cap(duration)
            assert len(clean.scenes) <= scene_cap(duration, split=edl.split_until is not None)


def test_scenes_keep_their_distance_after_sanitize():
    edl = story(broll=[])
    window_of = window_of_edl(edl)
    duration = edl.timeline().duration
    spans = [(6, 13), (8, 14), (17, 24), (20, 28), (36, 42), (40, 47), (46, 53)]
    clean = sanitize_visuals(sheet(scenes=[scene("phone", a, b) for a, b in spans]), edl)
    windows = [scene_window(s, window_of, duration) for s in clean.scenes]
    assert [s.word_from for s in clean.scenes] == [6, 36]
    assert all(w is not None for w in windows)
    assert windows[1][0] - windows[0][1] >= SCENE_MIN_GAP


def test_a_full_scene_keeps_the_first_items_that_pass():
    edl = story()
    items = [
        SceneItem(kind="chip", word_idx=37, text="20%"),  # выдуманное число — выпадает
        SceneItem(kind="chip", word_idx=36, text="18%"),
        SceneItem(kind="sticker", word_idx=38, sticker="bank"),
        SceneItem(kind="map", word_idx=40, text="Алматы"),
    ]
    raw = {
        "scenes": [
            {
                "layout": "card_top",
                "word_from": 36,
                "word_to": 42,
                "items": [i.model_dump() for i in items] + [{"kind": "photo", "word_idx": 41}],
            }
        ]
    }
    clean = sanitize_visuals(raw, edl)
    assert [i.kind for i in clean.scenes[0].items] == ["chip", "sticker", "map"]
    assert len(clean.scenes[0].items) == SCENE_ITEMS_MAX


# ------------------------------------------------------------------ образец


def test_sample_sheet_validates_clean():
    """Лист-образец обязан проходить правила как есть — иначе правила врут."""
    raw = json.loads((ROOT / "samples" / "edl-visuals.json").read_text(encoding="utf-8"))
    edl = load_edl(raw)
    visuals = Visuals.model_validate(raw["visuals"])
    assert not visuals.empty
    assert validate_visuals(edl, visuals) == []
    assert sanitize_visuals(visuals, edl) == visuals


# ------------------------------------------------------------------ числа без LLM


NUMBER_CASES = [
    ("квартира стоит 9 500 000 рублей", ["9 500 000 ₽"]),
    ("квартира стоит 9500000 рублей", ["9 500 000 ₽"]),
    ("отдал за неё 12500 рублей", ["12 500 ₽"]),
    ("цена всего 1500 рублей.", ["1500 ₽"]),
    ("это уже 11,5 миллионов", ["11,5 млн"]),
    ("это уже 11,5 миллионов рублей", ["11,5 млн ₽"]),
    ("заработал 500 тысяч за месяц", ["500 тыс."]),
    ("вложил 2 млн и не пожалел", ["2 млн"]),
    ("оборот 1,5 миллиарда тенге", ["1,5 млрд тенге"]),
    ("взял 100к в долг", ["100 тыс."]),
    ("стоит всего $300 в месяц", ["$300"]),
    ("стоит всего 300$ в месяц", ["$300"]),
    ("это 300 долларов в месяц", ["$300"]),
    ("до 3 миллионов долларов", ["до $3 млн"]),
    ("билет за 50 евро", ["€50"]),
    ("рост на 21 процент за год", ["21%"]),
    ("скидка 50% только сегодня", ["50%"]),
    ("ипотека под 18 процентов годовых", ["18%"]),
    ("ставка 12,5 процента", ["12,5%"]),
    ("доход вырос в 3 раза", ["×3"]),
    ("стало в 2,5 раза больше", ["×2,5"]),
    ("хожу туда 3 раза в неделю", ["3 раза"]),
    ("мы работаем уже 5 лет", ["5 лет"]),
    ("сделал это за 10 минут", ["10 минут"]),
    ("через 3 года продал", ["3 года"]),
    ("прошло 2 недели", ["2 недели"]),
    ("у нас 300 клиентов", ["300 клиентов"]),
    ("пришло 2 миллиона подписчиков", ["2 млн подписчиков"]),
    ("от 500 тысяч рублей", ["от 500 тыс. ₽"]),
    ("почти 90 процентов людей", ["почти 90%"]),
    ("в 2023 пришло 500 человек", ["500 человек"]),
    ("5 million dollars in 3 years", ["$5M", "3 years"]),
    ("сначала 100 рублей потом ещё 100 рублей", ["100 ₽"]),  # повтор не показываем дважды
]


@pytest.mark.parametrize("phrase,expected", NUMBER_CASES, ids=[c[0] for c in NUMBER_CASES])
def test_a_number_with_a_unit_becomes_a_chip(phrase, expected):
    edl = edl_of(phrase)
    found = number_chips(edl.words)
    assert [c.text for c in found] == expected
    assert all(len(c.text) <= CHIP_TEXT_MAX for c in found)
    # Честность своих же плашек: цифры те, что в речи, и плашка стоит на самом числе.
    said = digit_groups(phrase)
    assert all(overlay_rules.invented_numbers(c.text, said) == [] for c in found)
    assert all(any(ch.isdigit() for ch in edl.words[c.word_idx].text) for c in found)


NOT_NUMBERS = [
    "это было в 2024 году",
    "в 2024 мы начали",
    "к 1995 году всё изменилось",
    "звоните 8 800 555 35 35",
    "мой номер +7 999 123 45 67 запиши",
    "пиши на 89991234567 рублей не надо",
    "глава 3 называется так",
    "айфон 15 лучше",
    "в 10:30 начнём",
    "это 3-комнатная квартира",
    "занял 1-е место",
    "их было 500. Рублей при этом не было",
    "купил 2 3 раза подряд",
    "график 24/7 без выходных",
    "совсем без чисел тут",
]


@pytest.mark.parametrize("phrase", NOT_NUMBERS)
def test_years_phones_and_bare_numbers_stay_off_the_screen(phrase):
    assert number_chips(edl_of(phrase).words) == []


# ------------------------------------------------------------------ словарь стикеров


def lexicon_file() -> dict[str, Any]:
    return json.loads(LEXICON_PATH.read_text(encoding="utf-8"))


def test_lexicon_keys_are_sticker_keys_and_cover_the_whole_set():
    data = lexicon_file()
    keys = sticker_keys()
    assert keys, "набор стикеров пуст — индекс не прочитан"
    assert set(data["stickers"]) <= keys, "в словаре ключ, которого нет в наборе"
    assert set(data["stickers"]) == keys, "у стикера из набора нет ни одного слова"
    for key, entry in data["stickers"].items():
        assert set(entry) <= {"nouns", "adjectives", "exact"}, key
        assert any(entry.get(kind) for kind in ("nouns", "adjectives", "exact")), key
    assert len(load_lexicon()) >= 150


def test_a_lexicon_entry_belongs_to_one_sticker_only():
    owners: dict[str, set[str]] = {}
    for key, entry in lexicon_file()["stickers"].items():
        for kind in ("nouns", "adjectives", "exact"):
            listed = [normalize_word(w) for w in entry.get(kind, [])]
            assert len(listed) == len(set(listed)), f"{key}.{kind}: повтор"
            for word in listed:
                assert word, f"{key}.{kind}: пустая запись"
                owners.setdefault(word, set()).add(key)
    shared = {w: ks for w, ks in owners.items() if len(ks) > 1}
    assert not shared, f"слово записано за двумя стикерами: {shared}"


@pytest.mark.parametrize(
    "word,key",
    [
        ("дом", "house"), ("дома", "house"), ("домами", "house"), ("Квартиру,", "house"),
        ("деньгами", "money_bag"), ("денег", "money_bag"), ("рублей", "banknote"),
        ("целью", "target"), ("цели", "target"), ("стартапы", "rocket"), ("старта", "rocket"),
        ("важно", "exclamation"), ("важная", "exclamation"), ("бесплатно", "gift"),
        ("ужас", "shock"), ("ужасно", "thumbs_down"), ("неправильно", "cross"),
        ("правильно", "check"), ("цветы", "flower"), ("цвета", "palette"),
        ("папка", "folder"), ("папа", "family"), ("песня", "music"), ("долго", "hourglass"),
        ("плёнку", None), ("ребёнок", "baby"), ("чек-лист", "clipboard"),
        ("houses", "house"), ("Money!", "money_bag"), ("ideas", "bulb"),
    ],
)
def test_lexicon_matches_word_forms(word, key):
    assert load_lexicon().sticker_for(word) == key


@pytest.mark.parametrize(
    "word",
    [
        "который", "которая", "котлета",  # не «кот»
        "часть", "часто", "частный",  # не «час»
        "домен", "доминировать", "одомашнить", "управдом",  # не «дом»
        "банкрот", "банкет",  # не «банк»
        "целом", "целый", "целовать",  # не «цель»
        "стать", "дать", "брать", "идем", "идет", "семь", "почти", "судя", "судьба",
        "страница", "странно", "песок", "деревня", "шоколад", "страховка", "планета?",
        "ключевой", "кредитор", "игрушка", "мужчина", "женщина", "компот", "холодильник",
        "category", "keyboard", "start", "booking", "carpet", "planet!",
        "и", "в", "не", "что", "это", "как",
    ],
)
def test_stems_never_match_inside_unrelated_words(word):
    lexicon = load_lexicon()
    found = lexicon.sticker_for(word)
    # «планета» — честное слово для глобуса; остальное совпасть не должно.
    if normalize_word(word) in ("планета", "planet"):
        assert found == "globe"
    else:
        assert found is None, f"«{word}» получило стикер {found}"


def test_a_stem_matches_only_at_the_start_of_a_word_and_only_with_an_ending():
    """Свойство для КАЖДОЙ основы словаря, а не для десятка примеров."""
    lexicon = load_lexicon()
    for table, endings in ((lexicon.nouns, NOUN_ENDINGS), (lexicon.adjectives, ADJ_ENDINGS)):
        for stem, key in table.items():
            assert lexicon.sticker_for("ъъ" + stem) is None, f"«{stem}» нашлась в середине слова"
            assert lexicon.sticker_for(stem + "щщщ") is None, f"«{stem}» взяла чужой хвост"
            ending = sorted(endings)[-1]
            if stem + ending not in lexicon.exact and stem + ending not in lexicon.skip:
                assert lexicon.sticker_for(stem + ending) is not None, (stem, ending, key)


def test_lexicon_gives_candidates_in_speech_order():
    edl = edl_of("купил квартиру за большие деньги и сразу продал квартиру")
    assert [(o.word_idx, o.sticker) for o in lexicon_stickers(edl.words)] == [
        (0, "cart"), (1, "house"), (4, "money_bag"), (8, "house"),
    ]


# ------------------------------------------------------------------ бесплатный расстановщик


def test_fallback_on_the_story_is_valid_deterministic_and_has_no_scenes():
    edl = story()
    first = fallback_visuals(edl)
    assert validate_visuals(edl, first) == []
    assert fallback_visuals(edl) == first, "тот же лист монтажа — та же графика"
    assert first.scenes == [], "сцены — решение режиссёра, а не словаря"
    assert texts(first) == ["9 500 000 ₽", "3 года", "11,5 млн", "21%", "18%"]
    assert [o.seed for o in first.overlays] == list(range(len(first.overlays)))


def test_fallback_alternates_chip_styles():
    styles = [o.style for o in fallback_visuals(story()).overlays if o.kind == "chip"]
    assert styles == ["pill", "tag", "pill", "tag", "pill"]


def test_fallback_puts_stickers_on_dictionary_words_but_sparsely():
    edl = edl_of(
        "я купил квартиру и машину и телефон и ноутбук а потом продал машину и купил "
        "собаку потому что люблю собак и кошек и вообще природу и горы и лес а ещё "
        "люблю музыку и книги и кофе по утрам и пиццу по вечерам и самолёты и море"
    )
    found = fallback_visuals(edl)
    keys = [o.sticker for o in found.overlays if o.kind == "sticker"]
    assert len(keys) >= 2, "на словарные слова обязаны встать стикеры"
    assert len(keys) == len(set(keys)), "один предмет — один стикер"
    duration = edl.timeline().duration
    assert len(keys) <= max(1, int(duration // planner.FALLBACK_STICKER_EVERY))
    starts = [edl.words[o.word_idx].start for o in found.overlays if o.kind == "sticker"]
    assert all(b - a >= planner.FALLBACK_STICKER_EVERY for a, b in zip(starts, starts[1:]))


def test_a_sticker_key_is_spent_only_when_the_sticker_is_placed():
    """Первое «цель» не прошло по плотности — второе вправе получить мишень."""
    raw = json.loads((ROOT / "samples" / "edl-visuals.json").read_text(encoding="utf-8"))
    edl = load_edl(raw)
    found = fallback_visuals(edl)
    assert [(o.word_idx, o.sticker) for o in found.overlays] == [(1, "thinking"), (24, "target")]
    assert validate_visuals(edl, found) == []


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("Сколько стоит квартира в Алматы? Очень дорого стоит.", [("question", 0, 4)]),
        ("Ты правда так думаешь? Нет конечно.", [("question", 0, 3)]),
        ("А вот теперь самое интересное что мы узнали сегодня вечером? Да.", [("question", 5, 9)]),
        ("А вы уже успели попробовать этот новый удобный способ? Да.", [("question", 8, None)]),
        ("Почему так происходит. Никто не знает ответа.", [("question", 0, None)]),
        ("Это бомба! Просто слов нет.", [("exclaim", 0, None)]),
        ("И вот что я вам скажу друзья это просто невероятно! Да.", [("exclaim", 9, None)]),
        ("Что?! Не может быть.", [("question", 0, None)]),
        ("Я думаю что это нормально. И когда придёт время мы узнаем.", []),
    ],
)
def test_question_and_exclamation_give_decor(phrase, expected):
    marks = decor_marks(edl_of(phrase).words)
    assert [(m.glyph, m.word_idx, m.until_word_idx) for m in marks] == expected


def test_at_most_one_question_and_one_exclaim_per_reel():
    edl = edl_of("Зачем? Почему? Когда? Вот так! И вот так! И ещё вот так!")
    assert [m.glyph for m in decor_marks(edl.words)] == ["question", "exclaim"]


def test_fallback_ignores_words_that_were_cut_out():
    edl = story()
    cut_number = edl.model_copy(deep=True)
    # Вырезаем «18 процентов» (слова 36–37): плашки про 18% быть не должно.
    cut_number.cuts = [
        Cut(src_in=0.0, src_out=2.5, reason="hook"),
        Cut(src_in=2.5, src_out=3.0, reason="drop_filler"),
        Cut(src_in=3.0, src_out=18.0, reason="keep"),
        Cut(src_in=18.0, src_out=19.0, reason="drop_filler"),
        Cut(src_in=19.0, src_out=28.5, reason="keep"),
    ]
    found = fallback_visuals(cut_number)
    assert "18%" not in texts(found)
    assert validate_visuals(cut_number, found) == []


def test_fallback_on_an_empty_or_wordless_reel_is_empty():
    assert fallback_visuals(edl_of("ну вот и всё")) == Visuals()
    empty = EDL(source_duration=1.0, words=[], cuts=[], captions=[], preset="pulse")
    assert fallback_visuals(empty) == Visuals()


# ------------------------------------------------------------------ схема и промпт


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_schema_is_closed_everywhere():
    objects = [n for n in _walk(VISUALS_SCHEMA) if n.get("type") == "object"]
    assert len(objects) == 4, "ответ, элемент, сцена, элемент сцены"
    assert all(n.get("additionalProperties") is False for n in objects)


def test_schema_has_word_indices_and_no_time_fields():
    names = {name for n in _walk(VISUALS_SCHEMA) for name in (n.get("properties") or {})}
    assert {"word_idx", "until_word_idx", "word_from", "word_to"} <= names
    assert not names & {"start", "end", "time", "seconds", "duration", "at", "src_in", "src_out"}
    # Того, что ставит Python, модель не называет вовсе.
    assert not names & {"asset", "asset_aspect", "seed", "version"}


def test_schema_enums_come_from_the_sheet_types_and_the_sticker_set():
    props = VISUALS_SCHEMA["properties"]
    overlay = props["overlays"]["items"]["properties"]
    one_scene = props["scenes"]["items"]["properties"]
    item = one_scene["items"]["items"]["properties"]
    assert overlay["kind"]["enum"] == list(get_args(OverlayKind))
    assert overlay["style"]["enum"] == list(get_args(ChipStyle))
    assert overlay["slot"]["enum"] == list(get_args(Slot))
    assert overlay["glyph"]["enum"] == [*get_args(DecorGlyph), None]
    assert one_scene["layout"]["enum"] == list(get_args(SceneLayout))
    assert item["kind"]["enum"] == list(get_args(SceneItemKind))
    index_file = ROOT / "assets" / "stickers" / "index.json"
    index_keys = list(json.loads(index_file.read_text(encoding="utf-8"))["stickers"])
    assert len(index_keys) > 100
    assert overlay["sticker"]["enum"] == [*index_keys, None]
    assert item["sticker"]["enum"] == [*index_keys, None]
    assert one_scene["items"]["maxItems"] == SCENE_ITEMS_MAX
    assert overlay["text"]["maxLength"] == CHIP_TEXT_MAX


def test_prompt_file_parses_and_names_every_enum_value():
    prompt = load_prompt(VISUALS_PROMPT)
    assert prompt.system and prompt.user
    assert "<!-- system -->" not in prompt.system
    for literal in (OverlayKind, ChipStyle, DecorGlyph, Slot, SceneLayout, SceneItemKind):
        for value in get_args(literal):
            assert f"`{value}`" in prompt.system, f"промпт не называет значение {value!r}"


def test_rendered_prompt_names_every_sticker_and_every_limit_of_the_reel():
    prompt = load_prompt(VISUALS_PROMPT)
    variables = prompt_variables(story())
    system = render(prompt.system, variables)  # KeyError, если переменной не хватает
    user = render(prompt.user, variables)
    assert "{{" not in system and "{{" not in user
    for key, emoji in sticker_index().items():
        assert f"`{key}` {emoji}" in system
    assert "Потолок элементов (`overlays`): 7" in user
    assert "Потолок сцен (`scenes`): 2" in user
    assert "слова 44–47 — вставка" in user and "слова 31–34 — список" in user


def test_prompt_states_the_honesty_rule_and_forbids_timecodes():
    system = load_prompt(VISUALS_PROMPT).system.lower()
    assert "таймкод" in system and "индекс" in system
    assert "правило честности" in system
    assert "пустой ответ" in system


def _prompt_example() -> tuple[EDL, dict[str, Any]]:
    tail = load_prompt(VISUALS_PROMPT).system.split("## Пример", 1)[1]
    table = tail.split("```", 2)[1]
    answer = json.loads(tail.split("```json", 1)[1].split("```", 1)[0])
    rows = [re.match(r"\s*(\d+)\s+(\d+\.\d)\s+(\S+)", ln) for ln in table.strip().splitlines()]
    assert all(rows), "строка таблицы примера не разобралась"
    parsed = [(int(m[1]), float(m[2]), m[3]) for m in rows if m]
    words = [
        Word(
            idx=idx,
            text=text,
            start=start,
            end=round(min(start + 0.35, parsed[n + 1][1] if n + 1 < len(parsed) else 99.0), 3),
        )
        for n, (idx, start, text) in enumerate(parsed)
    ]
    total = words[-1].end
    edl = EDL(
        source_duration=total,
        words=words,
        cuts=[Cut(src_in=0.0, src_out=total, reason="keep")],
        captions=[],
        preset="pulse",
    )
    return edl, answer


def test_the_example_in_the_prompt_obeys_the_rules_it_teaches():
    """Пример — то, чему модель подражает первым делом. Он обязан быть годным."""
    edl, answer = _prompt_example()
    assert jsonshape.check(answer, VISUALS_SCHEMA) == []
    visuals = visuals_from_answer(answer)
    assert len(visuals.overlays) == len(answer["overlays"])
    assert len(visuals.scenes) == len(answer["scenes"]) == 1
    assert validate_visuals(edl, visuals) == []
    # И таблица примера выглядит ровно так, как её увидит модель.
    shown = load_prompt(VISUALS_PROMPT).system.split("## Пример", 1)[1].split("```", 2)[1]
    assert timed_words_table(edl) == shown.strip("\n")


def test_words_table_shows_kept_words_with_output_seconds():
    table = timed_words_table(story()).splitlines()
    assert table[0] == "   0    0.0  Сколько"
    assert table[4].startswith("   4    2.0  Алматы?")
    assert table[5] == "   6    2.5  мы", "вырезанное слово 5 модели не показывается"
    assert len(table) == 56


def test_no_scenes_are_offered_when_the_reel_cannot_hold_one():
    split = prompt_variables(story(split_until=6.0))
    assert split["max_scenes"] == "0"
    assert "слова 0–12 — кадр поделён на два видео" in split["busy"]
    assert prompt_variables(edl_of("раз два три четыре пять шесть семь"))["max_scenes"] == "0"
    assert prompt_variables(story())["busy"] != "нет"
    assert prompt_variables(edl_of("раз два три"))["busy"] == "нет"


def test_tier_is_one_the_client_knows():
    """Тир назван в шаге, а не в `schemas.TIERS`: клиент получает его аргументом."""
    assert Settings(_env_file=None, llm_provider="deepseek").model_for_tier(VISUALS_TIER)


# ------------------------------------------------------------------ вызов модели


def llm_cfg(**kw: Any) -> Settings:
    # _env_file=None: тест проверяет код, а не .env конкретной машины.
    return Settings(_env_file=None, llm_provider="deepseek", deepseek_api_key="sk-test", **kw)


def flag(on: bool = True) -> Settings:
    """Настройки с флагом `visuals_llm`. Поля в `Settings` пока нет — ставим мимо модели."""
    cfg = llm_cfg()
    object.__setattr__(cfg, "visuals_llm", on)
    return cfg


def client_with(*script: Any, **kw: Any) -> LLMClient:
    provider = FakeProvider([s if isinstance(s, BaseException) else result_of(s) for s in script])
    client = LLMClient(cfg=llm_cfg(**kw), provider=provider)
    client.fake = provider  # type: ignore[attr-defined]
    return client


def good_answer() -> dict[str, Any]:
    return {
        "overlays": [
            {"kind": "decor", "word_idx": 0, "until_word_idx": 4, "glyph": "question"},
            {"kind": "chip", "word_idx": 10, "until_word_idx": 13, "style": "tag",
             "text": "9 500 000 ₽"},
            {"kind": "chip", "word_idx": 26, "style": "pill", "text": "+21%", "emoji": "📈"},
            {"kind": "sticker", "word_idx": 49, "sticker": "bell", "slot": "top_right"},
        ],
        "scenes": [
            {"layout": "card_top", "word_from": 36, "word_to": 42, "heading": "Ипотека",
             "items": [
                 {"kind": "chip", "word_idx": 36, "text": "18%"},
                 {"kind": "sticker", "word_idx": 38, "sticker": "bank"},
             ]},
        ],
    }


async def test_good_answer_is_accepted_as_the_director_wrote_it():
    edl = story()
    llm = client_with(good_answer())
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=llm, cfg=flag(), report=report)

    assert len(llm.fake.calls) == 1
    assert report["from_llm"] is True
    assert validate_visuals(edl, got) == []
    assert [(o.kind, o.word_idx) for o in got.overlays] == [
        ("decor", 0), ("chip", 10), ("chip", 26), ("sticker", 49),
    ]
    assert got.overlays[1].style == "tag" and got.overlays[1].until_word_idx == 13
    assert got.overlays[2].emoji == "📈" and got.overlays[3].slot == "top_right"
    assert [o.seed for o in got.overlays] == [0, 1, 2, 3], "зерно — позиция в списке"
    assert [(s.layout, s.heading, len(s.items)) for s in got.scenes] == [("card_top", "Ипотека", 2)]


async def test_the_call_uses_the_prompt_file_the_schema_and_the_words_table():
    llm = client_with(good_answer())
    await plan_visuals(story(), llm=llm, cfg=flag())
    call = llm.fake.calls[0]
    assert call["schema"] is VISUALS_SCHEMA
    assert "  10    4.5  9" in call["user"], "модель видит индексы и секунды ролика"
    assert "Эээ" not in call["user"], "вырезанное слово в транскрипт не попадает"
    assert "Правило честности" in call["system"] and "`handshake`" in call["system"]
    assert llm.usages[0].prompt == VISUALS_PROMPT and llm.usages[0].tier == VISUALS_TIER


async def test_answer_over_the_caps_is_trimmed_without_a_retry():
    edl = story()
    answer = {
        "overlays": [
            {"kind": "decor", "word_idx": 0, "glyph": "question"},
            *(
                {"kind": "chip", "word_idx": w, "text": t}
                for w, t in ((10, "9 500 000 ₽"), (16, "3 года"), (21, "11,5 млн"), (26, "21%"),
                             (36, "18%"))
            ),
            *(
                {"kind": "sticker", "word_idx": w, "sticker": "house"}
                for w in (2, 24, 29, 34, 43)
            ),
        ],
        "scenes": [
            {"layout": "phone", "word_from": a, "word_to": b}
            for a, b in ((6, 13), (8, 14), (36, 42), (49, 54))
        ],
    }
    llm = client_with(answer)
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=llm, cfg=flag(), report=report)

    assert len(llm.fake.calls) == 1, "перебор срезается, а не оплачивается вторым вызовом"
    assert report["from_llm"] is True
    assert validate_visuals(edl, got) == []
    assert len(answer["overlays"]) == 11 > overlay_cap(edl.timeline().duration) == 7
    assert len(got.overlays) == 6
    assert len(got.scenes) == scene_cap(edl.timeline().duration) == 2
    # Сцены заняли слова 6–13 и 36–42: плашки на 10 и 36 им уступили, остальные три остались.
    assert texts(got) == ["3 года", "11,5 млн", "21%"]
    # Из пяти стикеров уцелели два: остальные стояли на времени сцены или вплотную к плашке.
    assert [o.word_idx for o in got.overlays if o.kind == "sticker"] == [29, 43]
    assert "question" in [o.glyph for o in got.overlays]


async def test_a_number_absent_from_the_transcript_is_dropped_and_the_rest_kept():
    edl = story()
    answer = {
        "overlays": [
            {"kind": "chip", "word_idx": 10, "text": "9 500 000 ₽"},
            {"kind": "chip", "word_idx": 21, "text": "12 млн"},
            {"kind": "chip", "word_idx": 26, "text": "рост 30%"},
            {"kind": "sticker", "word_idx": 49, "sticker": "bell"},
        ],
        "scenes": [],
    }
    llm = client_with(answer)
    got = await plan_visuals(edl, llm=llm, cfg=flag())
    assert len(llm.fake.calls) == 1
    assert texts(got) == ["9 500 000 ₽"]
    assert [o.sticker for o in got.overlays if o.kind == "sticker"] == ["bell"]


async def test_malformed_answer_costs_exactly_one_retry_that_carries_the_error():
    edl = story()
    llm = client_with("это не json", good_answer())
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=llm, cfg=flag(), report=report)
    assert len(llm.fake.calls) == 2, "должен быть ровно один ретрай"
    assert report["from_llm"] is True
    assert "Предыдущий ответ не подошёл" in llm.fake.calls[1]["user"]
    assert "не разобрался как JSON" in llm.fake.calls[1]["user"]
    assert texts(got) == ["9 500 000 ₽", "+21%"]


async def test_answer_off_the_schema_is_retried_with_the_shape_error():
    bad = {"overlays": [{"kind": "hologram", "word_idx": 10, "at": 4.5}], "scenes": []}
    llm = client_with(bad, good_answer())
    got = await plan_visuals(story(), llm=llm, cfg=flag())
    assert len(llm.fake.calls) == 2
    second = llm.fake.calls[1]["user"]
    assert "'hologram' не из набора" in second and "лишнее поле 'at'" in second
    assert len(got.overlays) == 4


async def test_two_malformed_answers_fall_back_without_raising():
    edl = story()
    llm = client_with("это не json", {"overlays": "ой", "scenes": []})
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=llm, cfg=flag(), report=report)
    assert len(llm.fake.calls) == 2, "второй провал — фолбэк, не третья попытка"
    assert report["from_llm"] is False
    assert got == fallback_visuals(edl)
    assert validate_visuals(edl, got) == [] and not got.empty


async def test_an_answer_with_nothing_usable_is_retried_with_the_rule_errors():
    """Модель попросила графику, и вся она — её же ошибки: говорим, что не так."""
    edl = story()
    useless = {
        "overlays": [
            {"kind": "chip", "word_idx": 21, "text": "12 млн"},
            {"kind": "sticker", "word_idx": 999, "sticker": "house"},
        ],
        "scenes": [],
    }
    llm = client_with(useless, good_answer())
    got = await plan_visuals(edl, llm=llm, cfg=flag())
    assert len(llm.fake.calls) == 2
    second = llm.fake.calls[1]["user"]
    assert "не звучит в речи" in second and "отсутствует в words" in second
    assert len(got.overlays) == 4

    twice = client_with(useless, useless)
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=twice, cfg=flag(), report=report)
    assert len(twice.fake.calls) == 2 and report["from_llm"] is False
    assert got == fallback_visuals(edl)


async def test_an_empty_answer_is_a_good_answer():
    """Режиссёр решил, что графики не надо, — словарь поверх его решения не запускается."""
    llm = client_with({"overlays": [], "scenes": []})
    report: dict[str, Any] = {}
    got = await plan_visuals(story(), llm=llm, cfg=flag(), report=report)
    assert got == Visuals() and report["from_llm"] is True
    assert len(llm.fake.calls) == 1


async def test_an_answer_lost_to_excess_only_is_not_retried():
    """Сцена в хвосте ролика — перебор по времени: секунды считает программа."""
    llm = client_with({"overlays": [], "scenes": [{"layout": "phone", "word_from": 50,
                                                  "word_to": 56}]})
    got = await plan_visuals(story(), llm=llm, cfg=flag())
    assert got == Visuals() and len(llm.fake.calls) == 1


async def test_a_board_left_without_content_is_dropped():
    answer = {
        "overlays": [{"kind": "chip", "word_idx": 37, "text": "проценты"}],
        "scenes": [
            {"layout": "card_top", "word_from": 36, "word_to": 42,
             "items": [{"kind": "chip", "word_idx": 36, "text": "25%"}]},
        ],
    }
    got = await plan_visuals(story(), llm=client_with(answer), cfg=flag())
    assert got.scenes == [], "доска без единого элемента — пустой кадр на три секунды"
    assert texts(got) == ["проценты"], "элемент, уступавший место сцене, вернулся"

    phone = {"overlays": [], "scenes": [{"layout": "phone", "word_from": 36, "word_to": 42}]}
    got = await plan_visuals(story(), llm=client_with(phone), cfg=flag())
    assert [s.layout for s in got.scenes] == ["phone"], "телефону содержимое не нужно"


async def test_provider_failure_falls_back_without_raising():
    edl = story()
    for failure in (ProviderError("сеть", retryable=False), RuntimeError("что-то сломалось")):
        llm = client_with(failure)
        report: dict[str, Any] = {}
        got = await plan_visuals(edl, llm=llm, cfg=flag(), report=report)
        assert got == fallback_visuals(edl)
        assert report["from_llm"] is False and len(llm.fake.calls) == 1


async def test_truncated_answer_is_not_paid_for_twice():
    """Обрыв по лимиту токенов повторять нельзя: тот же обрыв, вторая оплата."""
    provider = FakeProvider([result_of('{"overlays": [', stop_reason="length")])
    llm = LLMClient(cfg=llm_cfg(), provider=provider)
    got = await plan_visuals(story(), llm=llm, cfg=flag())
    assert len(provider.calls) == 1
    assert got == fallback_visuals(story())


async def test_without_llm_or_with_the_flag_off_the_provider_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
):
    edl = story()
    expected = fallback_visuals(edl)

    assert await plan_visuals(edl) == expected, "llm=None — бесплатный расстановщик"
    assert await plan_visuals(edl, llm=None, cfg=flag()) == expected

    # Пустой сценарий двойника: любой вызов уронил бы его.
    off = client_with()
    assert await plan_visuals(edl, llm=off, cfg=flag(False)) == expected
    assert await plan_visuals(edl, llm=off, cfg=llm_cfg()) == expected, "флага в настройках нет"
    # Без `cfg` шаг берёт настройки проекта. Подменяем их настройками без флага:
    # тест проверяет умолчание кода, а не то, что включено в .env этой машины.
    monkeypatch.setattr(planner, "default_settings", llm_cfg())
    assert await plan_visuals(edl, llm=off) == expected, "без флага модель не спрашивают"
    assert off.fake.calls == []

    not_ready = client_with(llm_enabled=False)
    assert await plan_visuals(edl, llm=not_ready, cfg=flag()) == expected
    assert not_ready.fake.calls == []


async def test_the_step_never_raises_into_the_pipeline(monkeypatch: pytest.MonkeyPatch):
    def boom(_edl: EDL) -> Visuals:
        raise RuntimeError("расстановщик сломан")

    monkeypatch.setattr(planner, "fallback_visuals", boom)
    report: dict[str, Any] = {}
    assert await plan_visuals(story(), report=report) == Visuals()
    assert report["from_llm"] is False
    empty = EDL(source_duration=1.0, words=[], cuts=[], captions=[], preset="pulse")
    assert await plan_visuals(empty, llm=client_with(), cfg=flag()) == Visuals()


async def test_the_result_always_passes_the_rules_whatever_the_model_says():
    """Итог шага — только лист без нарушений, каким бы ни был ответ."""
    edl = story()
    answers = [
        good_answer(),
        {"overlays": [{"kind": "chip", "word_idx": w, "text": f"{w} ₽"} for w in range(0, 57)],
         "scenes": []},
        {"overlays": [{"kind": "sticker", "word_idx": w, "emoji": "🔥"} for w in range(57)],
         "scenes": [{"layout": "paper", "word_from": w, "word_to": w + 8}
                    for w in range(0, 48, 4)]},
        {"overlays": [{"kind": "decor", "word_idx": 5, "glyph": "arrow"}], "scenes": []},
    ]
    for answer in answers:
        got = await plan_visuals(edl, llm=client_with(answer, answer), cfg=flag())
        assert validate_visuals(edl, got) == []
        assert sanitize_visuals(got, edl) == got
