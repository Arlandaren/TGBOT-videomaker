"""Договор о шрифте: рендер обязан объявлять шрифт так же, как линейка.

Раскладку строк считает Python, а сверяется она с `render/measure.mjs` — той
самой headless-страницей, про которую в шапке линейки написано «расхождение
здесь это расхождение в кадре». Утверждение верно ровно настолько, насколько
страница объявляет шрифт так же, как настоящий рендер.

Расхождение тут и жило. Линейка пишет `font-weight: 1 1000`, то есть один файл
закрывает весь диапазон и синтезировать жирность Chromium не станет. `Root.tsx`
же создавал `new FontFace("ReelbotCaption", url)` БЕЗ дескриптора веса — по
спецификации это `normal`, то есть 400, — а `Captions.tsx` просит
`fontWeight: 800`. На такую пару Chromium ВПРАВЕ нарисовать синтетический жир.

Замером проверено: на нынешней сборке он ширину не меняет (объявленные 400 и
100 против запрошенных 800 дали те же пиксели), то есть активной ошибки не
было — была несогласованность объявлений. Сторожим именно её: сверка 2 px
осмысленна ровно настолько, насколько рендер объявляет шрифт так же, как
страница замера. С чужими шрифтами, где собственный вес файла не совпадает с
весом пресета, цена этой несогласованности уже не предсказуема.

Проверка текстовая, как у пробника движения (`test_probe_drift.py`): исполнить
TSX вне композиции Remotion нечем, а число нужно сравнить.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULER = ROOT / "render" / "measure.mjs"
ROOT_TSX = ROOT / "render" / "src" / "Root.tsx"
CAPTIONS = ROOT / "render" / "src" / "layers" / "Captions.tsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ruler_weight_range() -> str:
    """`font-weight` из @font-face линейки."""
    m = re.search(r"@font-face\s*\{.*?font-weight:\s*([^;]+);", _src(RULER), re.S)
    assert m, "measure.mjs: в @font-face больше нет font-weight"
    return m.group(1).strip()


def test_the_renderer_declares_the_same_weight_range_as_the_ruler():
    """Иначе Python сверен с линейкой, а кадр нарисован синтетическим жиром."""
    declared = re.search(
        r"new FontFace\(\s*\"ReelbotCaption\",\s*`url\(\$\{staticFile\(fontUrl\)\}\)`\s*,"
        r"\s*\{([^}]*)\}\s*\)",
        _src(ROOT_TSX),
    )
    assert declared, (
        "Root.tsx: у `new FontFace(\"ReelbotCaption\", …)` нет третьего аргумента с "
        "дескрипторами. Без него лицо регистрируется весом 400, Captions.tsx просит "
        f"свой вес, и Chromium синтезирует жир — линейка объявляет {_ruler_weight_range()!r}"
    )
    m = re.search(r"weight:\s*\"([^\"]+)\"", declared.group(1))
    assert m, f"Root.tsx: в дескрипторах FontFace нет weight: {declared.group(1)!r}"
    assert m.group(1).strip() == _ruler_weight_range(), (
        f"диапазон веса разошёлся: Root.tsx {m.group(1)!r}, "
        f"measure.mjs {_ruler_weight_range()!r}. Меряется один шрифт, рисуется другой"
    )


def test_the_second_font_is_declared_the_same_way_as_the_first():
    """Второй шрифт темы (выделенное слово) — отдельное лицо с тем же диапазоном
    веса, что у первого и у линейки. Иначе пара сверена одним объявлением, а
    нарисована другим."""
    declared = re.search(
        r"new FontFace\(\s*\"ReelbotAccent\",\s*`url\(\$\{staticFile\(accentFontUrl\)\}\)`\s*,"
        r"\s*\{([^}]*)\}\s*\)",
        _src(ROOT_TSX),
    )
    assert declared, "Root.tsx: нет `new FontFace(\"ReelbotAccent\", …)` с дескрипторами"
    m = re.search(r"weight:\s*\"([^\"]+)\"", declared.group(1))
    assert m and m.group(1).strip() == _ruler_weight_range()
    ruler_faces = re.findall(r"font-family:\s*\"(ReelbotMeasure\w*)\";", _src(RULER))
    assert "ReelbotMeasureAccent" in ruler_faces, "measure.mjs не объявляет второй шрифт"


def test_the_accent_word_is_drawn_the_way_the_ruler_measures_it():
    """Выделенное слово: второй шрифт, вес из пресета и те же выключенные
    контекстные замены, что на странице замера.

    `calt` — не косметика: с ним Caveat рисует слово на 3-30 px шире, чем
    намерил Python (`test_the_production_block_rect_matches_chromium[marker]`)."""
    captions = _src(CAPTIONS)
    assert re.search(r"fontFamily:\s*`\"ReelbotAccent\"", captions)
    assert re.search(r"fontWeight:\s*accent\.weight", captions)
    in_render = re.search(r"ACCENT_FONT_FEATURES\s*=\s*'([^']+)'", captions)
    assert in_render, "Captions.tsx: нет ACCENT_FONT_FEATURES"
    assert re.search(r"fontFeatureSettings:\s*ACCENT_FONT_FEATURES", captions)
    in_ruler = re.search(r"fontFeatureSettings\s*=\s*'([^']+)'", _src(RULER))
    assert in_ruler and in_ruler.group(1) == in_render.group(1), (
        "выделенное слово в кадре и на линейке набрано с разными font-feature-settings"
    )


def test_the_caption_asks_for_the_weight_the_preset_names():
    """Вторая половина той же пары: кегль и вес субтитра берутся из пресета.

    Если `fontWeight` перестанет приходить из пресета, диапазон в объявлении
    шрифта станет бессмысленным — сверять будет нечего.
    """
    assert re.search(r"fontWeight:\s*ps\.font\.weight", _src(CAPTIONS)), (
        "Captions.tsx больше не берёт вес из пресета — проверьте, что сверка "
        "с линейкой всё ещё про тот же шрифт"
    )


# --------------------------------------------------- проводка выбора до рендера


def _harness(tmp_path, monkeypatch):
    """Прогон с подменёнными тяжёлыми этапами: нужен только план, что уедет в рендер."""
    import shutil
    from types import SimpleNamespace

    from src.core.edl import EDL, CaptionBlock, CaptionWord, Cut, Word
    from src.pipeline.asr import ASRResult
    from src.pipeline.probe import MediaInfo

    words = [Word(idx=i, text=f"сл{i}", start=i * 0.5, end=i * 0.5 + 0.4) for i in range(8)]
    edl = EDL(
        preset="pulse",
        source_duration=12.0,
        words=words,
        cuts=[Cut(src_in=0.0, src_out=12.0, reason="keep", framing="tight")],
        captions=[
            CaptionBlock(words=[CaptionWord(word_idx=w.idx) for w in words[:4]]),
            CaptionBlock(words=[CaptionWord(word_idx=w.idx) for w in words[4:]]),
        ],
    )
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\0" * 1024)
    info = MediaInfo(
        path="—", duration=12.0, width=1080, height=1920, fps=30.0, has_audio=True,
        audio_channels=2, audio_rate=48000, rotation=0, video_codec="h264", bitrate=0,
    )
    seen: dict[str, object] = {}

    async def fake_direct(*a, **k):
        return edl

    async def fake_metadata(*a, **k):
        return None

    async def fake_clean(ws, **k):
        return list(ws), 0

    async def fake_render(plan, out, **k):
        seen["plan"] = plan
        out.write_bytes(b"\0" * 64)

    monkeypatch.setattr("src.pipeline.job.probe", lambda *a, **k: info)
    monkeypatch.setattr("src.pipeline.job.is_playable", lambda *a, **k: True)
    monkeypatch.setattr("src.pipeline.job.apply_cuts", lambda e, s, d, **k: shutil.copy2(s, d))
    monkeypatch.setattr(
        "src.pipeline.job.autocrop", lambda s, d, **k: (shutil.copy2(s, d), d)[1]
    )
    monkeypatch.setattr(
        "src.pipeline.job.transcribe",
        lambda *a, **k: ASRResult(words=list(words), language="ru", backend="test", model="—"),
    )
    monkeypatch.setattr("src.pipeline.job.direct", fake_direct)
    monkeypatch.setattr("src.pipeline.job.make_metadata", fake_metadata)
    monkeypatch.setattr("src.pipeline.job.clean_transcript", fake_clean)
    monkeypatch.setattr("src.pipeline.job.measure_bg_luma", lambda *a, **k: None)
    monkeypatch.setattr("src.pipeline.job.arender_remotion", fake_render)
    return src, edl, seen, SimpleNamespace


async def test_the_chosen_font_reaches_the_render_plan(tmp_path, monkeypatch):
    """Выбор пользователя обязан доехать до плана — его читает рендерер."""
    from src.core.config import Settings
    from src.core.options import FONTS, JobOptions
    from src.pipeline.job import process_video

    src, _, seen, _ = _harness(tmp_path, monkeypatch)
    cfg = Settings(_env_file=None, audio_post_enabled=False)
    await process_video(
        src, tmp_path / "work", preset="pulse", cfg=cfg,
        options=JobOptions(font="serif", accent="red"),
    )

    plan = seen["plan"]
    assert plan.preset["font"]["file"] == FONTS["serif"][1].file
    assert plan.preset["font"]["family"] == FONTS["serif"][1].family
    assert plan.preset["colors"]["accent"] == "#FF3B30", "цвет не должен теряться из-за шрифта"


async def test_the_font_goes_into_the_edl_so_a_rerender_repeats_it(tmp_path, monkeypatch):
    """Инвариант 4: сохранённый лист обязан дать то же видео.

    Перерендер получает РОВНО лист — опции задачи до него не доезжают. Шрифт,
    оставшийся только в `jobs.options`, при смене темы молча сменился бы на
    шрифт темы, и ролик вышел бы другим.
    """
    from src.core.config import Settings
    from src.core.options import FONTS, JobOptions
    from src.pipeline.job import process_from_edl, process_video

    src, edl, seen, _ = _harness(tmp_path, monkeypatch)
    cfg = Settings(_env_file=None, audio_post_enabled=False)
    await process_video(
        src, tmp_path / "work", preset="pulse", cfg=cfg, options=JobOptions(font="narrow")
    )
    assert edl.font == "narrow", "шрифт не записан в монтажный лист"

    seen.clear()
    await process_from_edl(src, tmp_path / "again", edl, cfg=cfg)
    assert seen["plan"].preset["font"]["file"] == FONTS["narrow"][1].file


# --------------------------------------------------- второй шрифт до рендера


def test_the_second_font_file_travels_to_the_renderer(tmp_path):
    """Файл второго шрифта кладётся рядом с первым и уезжает в запрос рендера.

    Без него `Root.tsx` молча не объявит «ReelbotAccent», и выделенное слово
    нарисуется шрифтом строки — при раскладке, посчитанной под рукописный."""
    import json

    from src.core.config import Settings
    from src.core.edl import EDL, CaptionBlock, CaptionWord, Cut, Emphasis, Word
    from src.core.render_plan import build_plan
    from src.pipeline.render import RenderRequest, _request_payload, prepare_assets

    edl = EDL(
        preset="script",
        source_duration=3.0,
        words=[Word(idx=0, text="одну", start=0.2, end=0.6), Word(idx=1, text="истину", start=0.7, end=1.4)],
        cuts=[Cut(src_in=0.0, src_out=3.0, reason="keep")],
        captions=[
            CaptionBlock(
                words=[CaptionWord(word_idx=0), CaptionWord(word_idx=1, emphasis=Emphasis.ACCENT)]
            )
        ],
    )
    video = tmp_path / "in.mp4"
    video.write_bytes(b"\0" * 16)
    plan = build_plan(edl, str(video), width=1080, height=1920, fps=30)
    assert plan.preset["accent_font"]["file"] == "MarckScript-Regular.ttf"

    assets = tmp_path / "assets"
    plan = prepare_assets(plan, assets, cfg=Settings(_env_file=None))
    assert (assets / plan.meta["accent_font"]).is_file()
    assert plan.meta["accent_font"] != plan.meta["font"]

    payload = json.loads(
        _request_payload(RenderRequest(plan=plan, out=tmp_path / "o.mp4", assets_dir=assets, job_key="k"))
    )
    assert payload["plan"]["accentFontUrl"] == plan.meta["accent_font"]
    assert payload["plan"]["fontUrl"] == plan.meta["font"]


def test_a_theme_with_one_font_sends_no_second_font(tmp_path):
    import json

    from src.core.config import Settings
    from src.core.edl import EDL, CaptionBlock, CaptionWord, Cut, Word
    from src.core.render_plan import build_plan
    from src.pipeline.render import RenderRequest, _request_payload, prepare_assets

    edl = EDL(
        preset="glass",
        source_duration=3.0,
        words=[Word(idx=0, text="одну", start=0.2, end=0.6)],
        cuts=[Cut(src_in=0.0, src_out=3.0, reason="keep")],
        captions=[CaptionBlock(words=[CaptionWord(word_idx=0)])],
    )
    video = tmp_path / "in.mp4"
    video.write_bytes(b"\0" * 16)
    assets = tmp_path / "assets"
    plan = prepare_assets(
        build_plan(edl, str(video), width=1080, height=1920, fps=30), assets, cfg=Settings(_env_file=None)
    )
    payload = json.loads(
        _request_payload(RenderRequest(plan=plan, out=tmp_path / "o.mp4", assets_dir=assets, job_key="k"))
    )
    assert payload["plan"]["accentFontUrl"] is None


def test_the_gap_between_words_is_drawn_the_same_way_as_it_is_measured():
    """Пробел между словами — спан кеглем СТРОКИ, и рисует его кадр так же, как меряет линейка.

    Сверка 2 px идёт между Python и страницей `measure.mjs`; настоящий кадр
    рисует Captions.tsx. Если пробел в них устроен по-разному, сверка проходит,
    а кадр разъезжается — на ширину пробела в каждом промежутке.
    """
    from pathlib import Path

    render = Path(__file__).resolve().parents[1] / "render"
    captions = (render / "src" / "layers" / "Captions.tsx").read_text()
    measure = (render / "measure.mjs").read_text()

    # Кегль строки — самый мелкий кегль её слов (`geometry.line_size`).
    assert "fontSize: Math.min(...line.map((word) => wordSize(word, preset)))" in captions
    assert "Math.min(...words.map((word) => word.size || baseSize))" in measure
    # Разрядка и поправка к пробелу — в em от того же кегля, с обеих сторон.
    assert "letterSpacing: `${preset.font.letter_spacing}em`" in captions
    assert "wordSpacing: `${preset.font.word_spacing ?? 0}em`" in captions
    assert "gap.style.letterSpacing = `${letterSpacing}em`" in measure
    assert "gap.style.wordSpacing = `${wordSpacing}em`" in measure
    # И это отдельный спан с пробелом, а не голый текст в контейнере строки.
    assert '<span style={gapStyle(line, preset)}>{" "}</span>' in captions
    assert 'gap.textContent = " "' in measure
