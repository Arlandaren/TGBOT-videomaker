"""Опции монтажа и пакеты звёзд: то, что пользователь выбирает в Mini App."""

from __future__ import annotations

import pytest

from src.core.config import Settings
from src.core.options import ACCENTS, FONTS, JobOptions, apply_accent, apply_font
from src.core.preset import load_preset


def test_a_job_without_options_behaves_as_before():
    """Задачи из чата и из времени до Mini App опций не несут — всё включено."""
    o = JobOptions.from_stored(None)
    assert (o.accent, o.cut, o.sfx, o.broll, o.camera) == ("theme", True, True, True, True)


def test_an_unknown_accent_falls_back_to_the_theme():
    """Палитру в приложении могут урезать, а старые задачи в БД останутся.

    Упасть на такой задаче значило бы потерять оплаченный ролик из-за переименования
    цвета, поэтому неизвестный ключ читается как «цвет темы».
    """
    assert JobOptions.from_stored({"accent": "ultraviolet"}).accent == "theme"


def test_unknown_fields_from_a_newer_client_are_ignored():
    assert JobOptions.from_stored({"cut": False, "hook": True}).cut is False


@pytest.mark.parametrize("key", [k for k, (_, color) in ACCENTS.items() if color])
def test_the_accent_recolours_both_highlight_colours(key: str):
    glass = load_preset("glass")
    painted = apply_accent(glass, key)
    assert painted.colors.accent == painted.colors.shout == ACCENTS[key][1]
    # Исходный пресет — общий объект кеша, трогать его нельзя.
    assert glass.colors.accent == "#F3D28F"


def test_the_theme_accent_leaves_the_preset_untouched():
    bold = load_preset("bold")
    assert apply_accent(bold, "theme") is bold


# ------------------------------------------------------------------ шрифты


def test_a_job_without_options_keeps_the_theme_font():
    assert JobOptions.from_stored(None).font == "theme"


def test_an_unknown_font_falls_back_to_the_theme():
    """Та же причина, что и у цвета: набор шрифтов может измениться, а задачи
    со старым ключом остаться в БД. Падать на них нельзя."""
    assert JobOptions.from_stored({"font": "comic-sans"}).font == "theme"


def test_the_theme_font_leaves_the_preset_untouched():
    bold = load_preset("bold")
    assert apply_font(bold, "theme") is bold


@pytest.mark.parametrize("key", [k for k, (_, choice) in FONTS.items() if choice])
def test_the_chosen_font_reaches_the_preset(key: str):
    glass = load_preset("glass")
    swapped = apply_font(glass, key)
    choice = FONTS[key][1]
    assert swapped.font.family == choice.family
    assert swapped.font.file == choice.file
    assert swapped.font.weight == choice.weight
    # Исходный пресет — общий объект кеша.
    assert glass.font.file == "Montserrat-ExtraBold.ttf"


@pytest.mark.parametrize("key", [k for k, (_, choice) in FONTS.items() if choice])
def test_the_chosen_font_keeps_the_optical_size(key: str):
    """Кегль задан в пикселях, а глаз меряет высоту прописных.

    У Oswald SemiBold она 0.810 em против 0.700 у Montserrat: те же 88 px дали
    бы букву на 16 % крупнее задуманного темой. Поэтому кегль пересчитывается
    по высоте прописных из ВЫГРУЖЕННЫХ метрик — тех самых, что сверены с
    Chromium, а не по числу, переписанному руками в каталог.
    """
    from src.core.geometry import load_metrics

    glass = load_preset("glass")
    swapped = apply_font(glass, key)
    base, new = load_metrics(glass.font.file), load_metrics(swapped.font.file)
    ratio = (base.cap_height / base.units_per_em) / (new.cap_height / new.units_per_em)

    for got, was in (
        (swapped.font.size, glass.font.size),
        (swapped.caption.quiet.font_size, glass.caption.quiet.font_size),
        (swapped.caption.title_card.font_size, glass.caption.title_card.font_size),
    ):
        assert got == max(12, int(was * ratio)), f"кегль не нормирован: {got} вместо {was}×{ratio:.3f}"

    optical = swapped.font.size * (new.cap_height / new.units_per_em)
    want = glass.font.size * (base.cap_height / base.units_per_em)
    assert abs(optical - want) <= 1.0, (
        f"высота прописной уехала: {optical:.1f} px вместо {want:.1f}"
    )


def test_every_catalogued_font_is_actually_in_the_repository():
    """Каталог без файла даёт валидный mp4 системным шрифтом: `prepare_assets`
    на отсутствие пишет warning, а `Root.tsx` молча продолжает. Молчат оба, и
    раскладка при этом посчитана по другому шрифту."""
    from src.core.config import REPO_ROOT
    from src.core.geometry import load_metrics

    for key, (_, choice) in FONTS.items():
        if choice is None:
            continue
        ttf = REPO_ROOT / "assets" / "fonts" / choice.file
        assert ttf.exists(), f"{key}: нет файла {choice.file}"
        metrics = load_metrics(choice.file)
        assert metrics.family == choice.family, (
            f"{key}: в каталоге семейство {choice.family!r}, а в шрифте {metrics.family!r} — "
            "ASS ищет шрифт по имени семейства и возьмёт не тот файл"
        )

    # Темы называют шрифт сами, мимо каталога: у `press` это было записано
    # руками. Проверка та же и по той же причине — libass ищет по семейству, а
    # не по файлу, и опечатка даёт валидный mp4 чужим шрифтом.
    from src.core.preset import available, load_preset

    for name in available():
        font = load_preset(name).font
        ttf = REPO_ROOT / "assets" / "fonts" / (font.file or "")
        assert font.file and ttf.exists(), f"{name}: нет файла {font.file!r}"
        assert load_metrics(font.file).family == font.family, (
            f"{name}: тема просит семейство {font.family!r}, "
            f"а в файле {load_metrics(font.file).family!r}"
        )
        # Второй шрифт темы — тот же договор. Имя семейства у него обязано быть
        # СВОИМ: наклонное начертание под именем прямого libass спутал бы.
        second = load_preset(name).accent_font
        if second is None:
            continue
        assert (REPO_ROOT / "assets" / "fonts" / second.file).exists(), (
            f"{name}: нет файла второго шрифта {second.file!r}"
        )
        assert load_metrics(second.file).family == second.family, (
            f"{name}: второй шрифт назван {second.family!r}, "
            f"а в файле {load_metrics(second.file).family!r}"
        )
        assert second.family != font.family, f"{name}: два шрифта под одним семейством"


def test_a_chosen_font_switches_the_second_font_off():
    """Выбранный шрифт — это «вся надпись этим шрифтом».

    Оставь пару включённой, и выбор менял бы только мелкие слова: самое заметное
    слово блока так и осталось бы рукописным."""
    from src.core.preset import available

    paired = [n for n in available() if load_preset(n).accent_font is not None]
    assert paired
    for name in paired:
        base = load_preset(name)
        assert apply_font(base, "theme").accent_font is not None
        chosen = apply_font(base, "serif")
        assert chosen.accent_font is None
        assert chosen.font.family == FONTS["serif"][1].family
        # Исходная тема не тронута: `load_preset` кеширует объект.
        assert load_preset(name).accent_font is not None


def test_the_second_font_needs_a_plain_line():
    """Разрядка и капс строки ломают рукописное слово — запрещены данными."""
    import pytest
    from pydantic import ValidationError

    from src.core.preset import Preset

    data = load_preset("script").model_dump()
    for field, value in (("letter_spacing", 0.05), ("uppercase", True)):
        broken = {**data, "font": {**data["font"], field: value}}
        with pytest.raises(ValidationError):
            Preset(**broken)


def test_quiet_and_title_registers_keep_one_font():
    base = load_preset("script")
    assert base.caption.quiet.preset(base).accent_font is None
    assert base.caption.title_card.preset(base).accent_font is None


def test_the_font_and_the_accent_compose():
    """Пользователь выбирает и шрифт, и цвет — одно не должно затирать другое."""
    glass = load_preset("glass")
    both = apply_font(apply_accent(glass, "red"), "serif")
    assert both.colors.accent == ACCENTS["red"][1]
    assert both.font.file == FONTS["serif"][1].file


def test_token_packages_parse_from_the_env_string():
    cfg = Settings(_env_file=None, token_packages=(
        '[{"tokens": 1170, "stars": 3599}, {"tokens": 240, "stars": 749, "rub": 1099}]'
    ))
    assert [(p.tokens, p.stars, p.rub) for p in cfg.token_packages] == [(240, 749, 1099), (1170, 3599, 0)]


@pytest.mark.parametrize("raw", [
    '[{"tokens": 0, "stars": 50}]', '[{"tokens": 240, "stars": 0}]', '[{"tokens": -1, "stars": 50}]',
    '[{"tokens": 240, "stars": 1}, {"tokens": 240, "stars": 2}]',
])
def test_a_free_empty_or_duplicate_package_is_a_config_error(raw: str):
    with pytest.raises(ValueError):
        Settings(_env_file=None, token_packages=raw)


def test_the_old_star_packages_setting_is_ignored():
    """STAR_PACKAGES на сервере («1 → 50 ⭐») не имеет права стать «1 токен за
    50 звёзд»: у пакетов токенов своя настройка."""
    cfg = Settings(_env_file=None, star_packages='{"1": 50}')
    assert [p.tokens for p in cfg.token_packages] == [240, 590, 1170]


def test_a_new_job_always_uses_the_font_of_the_theme():
    """Сохранённый раньше шрифт не действует на новые ролики; старые задачи
    читаются как есть — перерендер обязан дать тот же ролик."""
    assert JobOptions.for_new_job({"font": "serif", "accent": "red"}).font == "theme"
    assert JobOptions.for_new_job({"font": "serif", "accent": "red"}).accent == "red"
    assert JobOptions.for_new_job(None).font == "theme"
    assert JobOptions.from_stored({"font": "serif"}).font == "serif"
