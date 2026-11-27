"""Опции монтажа и пакеты звёзд: то, что пользователь выбирает в Mini App."""

from __future__ import annotations

import pytest

from src.core.config import Settings
from src.core.options import (
    ACCENTS,
    FONTS,
    REMEMBERED,
    JobOptions,
    apply_accent,
    apply_font,
    apply_grade,
    available_switches,
    job_settings,
)
from src.core.preset import load_preset

# Переключатели, добавленные 27.11.2026.
NEW_SWITCHES = ("studio", "tracking", "transitions", "visuals", "title", "progress", "grade", "smooth")


def test_a_job_without_options_behaves_as_before():
    """Задачи из чата и из времени до Mini App опций не несут — всё включено."""
    o = JobOptions.from_stored(None)
    assert (o.accent, o.cut, o.sfx, o.broll, o.camera) == ("theme", True, True, True, True)


def test_the_new_switches_are_on_for_a_job_that_never_heard_of_them():
    """Задача из чата и задача до появления переключателей: всё как было."""
    o = JobOptions.from_stored(None)
    assert all(getattr(o, name) is True for name in NEW_SWITCHES)
    assert all(getattr(JobOptions.for_new_job({}), name) is True for name in NEW_SWITCHES)


def test_default_options_leave_the_server_settings_alone():
    """Ничего не выключено — настройки тот же объект, а не копия: этапы обязаны
    видеть ровно то, что задано на сервере."""
    cfg = Settings(_env_file=None)
    assert job_settings(cfg, JobOptions()) is cfg


def test_each_switch_turns_its_own_stage_off_and_nothing_else():
    # 60 кадров заданы явно: умолчание сервера — отдельное решение, и тест про
    # галочку от него зависеть не должен.
    cfg = Settings(_env_file=None, output_fps=60)
    expected = {
        "studio": {"voice_studio": False, "voice_highpass_hz": 0.0, "voice_denoise_db": 0.0},
        "tracking": {"crop_follow": False},
        "transitions": {"transitions_enabled": False},
        "visuals": {"visuals_enabled": False},
        "progress": {"progress_bar": False},
        "smooth": {"output_fps": 0},
    }
    watched = sorted({key for change in expected.values() for key in change})
    for name, change in expected.items():
        got = job_settings(cfg, JobOptions(**{name: False}))
        for key in watched:
            want = change.get(key, getattr(cfg, key))
            assert getattr(got, key) == want, f"{name}: {key}={getattr(got, key)!r}"
    # Настройки сервера — общий объект процесса: выключенное у одного ролика не
    # имеет права остаться выключенным у следующего.
    assert cfg.voice_studio and cfg.crop_follow and cfg.visuals_enabled and cfg.output_fps == 60


def test_a_switch_never_turns_on_what_the_server_turned_off():
    """Галочка умеет только выключать: графика, снятая владельцем, и 60 кадров,
    которым на сервере нет памяти, роликом не включаются."""
    cfg = Settings(
        _env_file=None, visuals_enabled=False, transitions_enabled=False, progress_bar=False,
        voice_studio=False, crop_follow=False, output_fps=0,
    )
    got = job_settings(cfg, JobOptions())
    assert not (got.visuals_enabled or got.transitions_enabled or got.progress_bar)
    assert not (got.voice_studio or got.crop_follow) and got.output_fps == 0


def test_the_app_is_offered_only_the_switches_that_change_something():
    """Сервер, где приём выключен, не показывает его галочку: включить его
    роликом нельзя, и галочка не меняла бы ничего. Так на проде с 60 кадрами:
    там `OUTPUT_FPS=0`."""
    base = {"_env_file": None, "openai_api_key": "k", "llm_provider": "openai", "output_fps": 60}
    assert available_switches(Settings(**base)) == list(REMEMBERED)

    def missing(**kw) -> set[str]:
        cfg = Settings(**{**base, **kw})
        return set(REMEMBERED) - set(available_switches(cfg))

    assert missing(output_fps=0) == {"smooth"}
    assert missing(visuals_enabled=False) == {"visuals"}
    assert missing(transitions_enabled=False) == {"transitions"}
    assert missing(progress_bar=False) == {"progress"}
    assert missing(crop_follow=False) == {"tracking"}
    assert missing(broll_enabled=False) == {"broll"}
    assert missing(audio_post_enabled=False) == {"sfx", "studio"}
    assert missing(voice_studio=False) == set(), "без студийных звеньев галочка ещё снимает чистку"
    assert missing(voice_studio=False, voice_denoise_db=0, voice_highpass_hz=0) == {"studio"}
    assert missing(render_backend="ass") == {"transitions", "visuals", "progress"}
    assert missing(openai_api_key="") == {"title"}, "заголовок сочиняет модель"


def test_an_old_job_reads_its_transitions_from_the_camera_switch():
    """До своей галочки переходы выключались «Движением камеры». Перерендер
    задачи тех времён обязан дать тот же ролик: без камеры — без переходов."""
    assert JobOptions.from_stored({"camera": False}).transitions is False
    assert JobOptions.from_stored({"camera": True}).transitions is True
    assert JobOptions.from_stored({"camera": False, "transitions": True}).transitions is True
    # Новая задача: галочки независимы.
    assert JobOptions.for_new_job({"camera": False}).transitions is True


def test_the_grade_switch_turns_the_colour_correction_off():
    from src.pipeline.crop import grade_filter

    glass = load_preset("glass")
    assert grade_filter(glass.grade), "у glass цветокор есть — иначе тест ничего не проверяет"
    assert apply_grade(glass, True) is glass
    plain = apply_grade(glass, False)
    assert grade_filter(plain.grade) == ""
    assert load_preset("glass").grade.enabled, "исходная тема — общий объект кеша"


def test_what_is_remembered_between_reels():
    """Запоминаются переключатели второго шага, нарезка — нет: «без нарезки»
    выбирают под конкретное видео, и запомненный отказ молча оставлял бы паузы
    в роликах, присланных в чат."""
    kept = JobOptions(studio=False, cut=False).remembered()
    assert set(kept) == set(REMEMBERED) and "cut" not in kept
    assert kept["studio"] is False and kept["sfx"] is True
    assert set(NEW_SWITCHES) <= set(REMEMBERED)
    # Запомненное действует на следующий ролик — и из приложения, и из чата.
    again = JobOptions.for_new_job({"accent": "red", **kept})
    assert again.studio is False and again.cut is True


def test_every_switch_has_a_toggle_and_texts_in_the_app():
    """Переключатель без строки в приложении выключить нечем, строка без поля
    ничего не меняет. Сверяем разметку и словарь с моделью."""
    import re
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "src" / "webapp" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    app = (static / "app.js").read_text(encoding="utf-8")
    toggles = set(re.findall(r'data-option="(\w+)"', html))
    assert toggles == set(REMEMBERED), toggles ^ set(REMEMBERED)
    assert toggles <= set(JobOptions.model_fields)
    for name in toggles:
        assert f'data-help="{name}"' in html, name
        for key in (f'"opt.{name}"', f'"help.{name}"'):
            assert app.count(key) == 2, f"{key}: нужен в обоих языках"


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


@pytest.mark.parametrize("key", [k for k in FONTS if k != "theme"])
def test_a_chosen_font_never_sets_words_far_apart(key: str):
    """Старая задача с выбранным шрифтом при пересборке не разводит слова.

    У моноширинного шрифта пробел шириной с букву (0.6 em): ролики с ним
    читались как отдельные слова. Поправка темы к пробелу подобрана под ЕЁ
    шрифт, поэтому у выбранного шрифта она своя — до обычных 0.28 em.
    """
    from src.core.geometry import metrics_for, space_width

    for theme in ("pulse", "deco_line"):
        preset = apply_font(load_preset(theme), key)
        gap = space_width(
            1.0,
            metrics=metrics_for(preset),
            letter_spacing=preset.font.letter_spacing,
            word_spacing=preset.font.word_spacing,
        )
        assert gap <= 0.30 + 1e-9, f"{theme} + {key}: зазор {gap:.3f} em"
        assert preset.font.word_spacing <= 0
