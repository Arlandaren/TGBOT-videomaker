"""Графика против настоящего рендерера: коробка плашки и окно сцены.

Два места, где Python и рендерер обязаны сойтись числом, а не «примерно»:

1. Коробка плашки. Её считает Python по метрикам шрифта (`overlays.measure_chip`)
   и по ней же разводит плашку с субтитром и лицом, а рисует браузер. Сверка —
   линейкой `render/measure.mjs` в том самом Chromium, которым идёт рендер, тем
   же шрифтом из `assets/fonts`; допуск тот же, что у субтитра и плашки пункта
   (`tests/test_list_items.py`): два пикселя.
2. Окно говорящего во время сцены. Формулы записаны в шапке
   `overlay_models.py`, считают их двое: `overlays.stage_at` и `stageAt` из
   `render/src/visuals/motion.mjs`. По окну Python пересчитывает трек лица, и
   разойдись копии хоть на кадр — лицо в окне окажется не там, где его ждут.

Оба теста медленные: первому нужен браузер, второму node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.core.config import Settings
from src.core.edl import EDL, CaptionBlock, CaptionWord, Word
from src.core.geometry import Rect, metrics_for
from src.core.overlay_models import PlanScene, PlanSceneBg, Scene, Visuals
from src.core.overlays import dressed_plan, measure_chip, stage_at
from src.core.preset import load_preset
from src.core.render_plan import build_plan

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "render"
MEASURE = RENDER / "measure.mjs"
PROBE = RENDER / "overlay_probe.mjs"
MOTION = RENDER / "src" / "visuals" / "motion.mjs"

# Темы подобраны под то, чем плашка может разойтись с браузером: жирный
# гротеск с отрицательной разрядкой (pulse), верхний регистр (bold, boardwalk),
# широкая разрядка, срезанная потолком плашки (deco_line, chisel), моноширинный
# шрифт с поправкой к пробелу (notebook), пара шрифтов (cursive) и антиква
# (press, editorial). У chisel, deco_line, cursive, editorial и press в шрифте
# нет знака рубля — там меряется его подмена словом.
THEMES = ["pulse", "bold", "chisel", "deco_line", "notebook", "cursive", "press", "editorial"]

# (текст, вид, значок). Цена с рублём — главный случай плашки; минус, процент
# и скобки — знаки, которые субтитр срезает, а плашка обязана набрать.
CHIPS = [
    ("9 500 000 ₽", "tag", None),
    ("9 500 000 ₽", "pill", "💰"),
    ("одна истина", "pill", "💡"),
    ("одна истина", "pill", None),
    ("−30 % за год", "tag", None),
    ("(до 5 лет)", "label", None),
    ("факт", "label", None),
    ("одна цель", "heading", None),
    ("Как это работает", "heading", None),
]


def cfg() -> Settings:
    return Settings(_env_file=None, bot_token="x", render_fps=30)


def node() -> str:
    found = shutil.which("node")
    if found is None or not (RENDER / "node_modules").exists():
        pytest.skip("нужен node и render/node_modules")
    return found


def measure_in_chromium(request: dict) -> dict:
    """Запрос линейке `measure.mjs`; нет браузера — пропуск, а не провал."""
    done = subprocess.run(
        [node(), str(MEASURE)],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=str(RENDER),
    )
    if done.returncode != 0:
        text = done.stderr.lower()
        # «Браузера нет» и «кадр разъехался» — разные новости (`test_geometry`).
        if any(
            mark in text
            for mark in ("openbrowser", "ensurebrowser", "browser executable", "chrome headless")
        ):
            pytest.skip(f"нет скачанного Chromium: {done.stderr.strip()[-300:]}")
        pytest.fail(f"measure.mjs упал: {done.stderr.strip()[-2000:]}")
    return json.loads(done.stdout)


# Каждая тема — на полном кадре; на кадре превью (1280) — две: кегли там
# округляются иначе (`int` от доли), и ошибиться можно именно в округлении, а
# оно от темы не зависит. Запуск браузера на каждый кегль стоит секунд, поэтому
# все темы на оба размера не гоняются.
CASES = [(name, 1920) for name in THEMES] + [("pulse", 1280), ("chisel", 1280)]


@pytest.mark.slow
@pytest.mark.parametrize("name, height", CASES, ids=[f"{n}-{h}" for n, h in CASES])
def test_chromium_draws_the_chip_box_the_plan_promised(name: str, height: int):
    """Коробка плашки в плане и в настоящем Chromium — в пределах 2 px.

    Прямоугольник плашки в плане — это ровно её коробка: рендерер ставит текст
    по центру и ничего не меряет сам. Значит ошибка замера — это текст,
    вылезший из плашки, или плашка, наехавшая на субтитр.
    """
    preset = load_preset(name).scaled(height)
    font = cfg().fonts_dir / preset.font.file
    chips = []
    for n, (text, style, emoji) in enumerate(CHIPS):
        made = measure_chip(text, style, preset, height, emoji=emoji)
        assert made is not None, (name, text, style)
        chips.append((f"c{n}", made))

    # У линейки кегль контейнера один на запрос, а от него считаются струт
    # строки и разрядка в em. Поэтому запрос — на каждый кегль свой.
    by_size: dict[tuple, list] = {}
    for key, made in chips:
        look = made.look
        group = (
            look["font_size"],
            look["letter_spacing"],
            look["word_spacing"],
            look["line_height"],
        )
        by_size.setdefault(group, []).append((key, made))

    measured: dict[str, dict] = {}
    for (size, letter, word, line), group in by_size.items():
        request = {
            "font": str(font),
            "weight": preset.font.weight,
            "base_size": size,
            "letter_spacing": letter,
            "word_spacing": word,
            "line_height": line,
            "items": [],
            "boxes": [
                {
                    "id": key,
                    "lines": [[{"text": w, "size": size} for w in made.look["words"]]],
                    "padding_x": made.look["padding_x"],
                    "padding_y": made.look["padding_y"],
                    "border_width": 0,
                    "icon_room": made.look["emoji_box"] + made.look["emoji_gap"],
                    "icon_box": made.look["emoji_box"],
                    "icon_size": made.look["emoji_size"],
                }
                for key, made in group
            ],
        }
        measured.update(measure_in_chromium(request)["boxes"])

    for key, made in chips:
        what = (name, height, made.look["style"], made.look["words"])
        assert measured[key]["width"] == pytest.approx(made.width, abs=2.0), what
        assert measured[key]["height"] == pytest.approx(made.height, abs=2.0), what


@pytest.mark.slow
def test_the_ruble_fallback_is_what_keeps_the_box_honest():
    """Знак, которого нет в шрифте, Chromium рисует чужим шрифтом — мимо замера.

    Ради этого рубль и подменяется словом: тест показывает, что сама подмена
    меряется точно, и что набор тем действительно содержит шрифт без рубля
    (иначе проверка выше про него молчала бы).
    """
    preset = load_preset("chisel").scaled(1920)
    assert "₽" not in metrics_for(preset).advances
    made = measure_chip("9 500 000 ₽", "tag", preset, 1920)
    assert made.look["words"][-1] == "РУБ."
    look = made.look
    request = {
        "font": str(cfg().fonts_dir / preset.font.file),
        "weight": preset.font.weight,
        "base_size": look["font_size"],
        "letter_spacing": look["letter_spacing"],
        "word_spacing": look["word_spacing"],
        "line_height": look["line_height"],
        "items": [],
        "boxes": [
            {
                "id": "spelled",
                "lines": [[{"text": w, "size": look["font_size"]} for w in look["words"]]],
                "padding_x": look["padding_x"],
                "padding_y": look["padding_y"],
            }
        ],
    }
    box = measure_in_chromium(request)["boxes"]["spelled"]
    assert box["width"] == pytest.approx(made.width, abs=2.0)
    assert box["height"] == pytest.approx(made.height, abs=2.0)


# ------------------------------------------------------------------ окно сцены


def talk(n: int = 90) -> EDL:
    words = [
        Word(idx=i, text=f"сл{i}", start=round(i * 0.35, 3), end=round(i * 0.35 + 0.3, 3))
        for i in range(n)
    ]
    captions = [
        CaptionBlock(words=[CaptionWord(word_idx=j) for j in range(i, min(i + 3, n))])
        for i in range(0, n, 3)
    ]
    return EDL(source_duration=n * 0.35, words=words, cuts=[], captions=captions, preset="pulse")


def probe_scenes(plan_path: Path) -> dict:
    """Покадровые ряды сцен из пробника рендерера (`overlay_probe.mjs --plan`)."""
    if not PROBE.exists() or not MOTION.exists():
        pytest.skip(
            f"нет {PROBE.name} или {MOTION.name}: пробник движения графики ещё не написан "
            f"(пакет рендерера), сверять `stage_at` не с чем"
        )
    done = subprocess.run(
        [node(), str(PROBE), "--json", "--plan", str(plan_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=str(RENDER),
    )
    if done.returncode != 0:
        pytest.fail(f"{PROBE.name} упал: {done.stderr.strip()[-2000:]}")
    return json.loads(done.stdout)


KEYS = ("p", "x", "y", "width", "height", "radius", "tilt", "scale", "tx", "ty")


def assert_same_stage(report: dict, scenes: list[dict]) -> None:
    fps, width, height = report["fps"], report["width"], report["height"]
    assert len(report["scenes"]) == len(scenes) > 0
    for row, scene in zip(report["scenes"], scenes):
        frames = range(row["first_frame"], row["first_frame"] + len(row["p"]))
        # Пробник снимает и кадры до начала и после конца: там сцены нет.
        assert frames[0] < row["f0"] and frames[-1] > row["f1"]
        for n, frame in enumerate(frames):
            ours = stage_at(scene, frame, fps, width, height)
            for key in KEYS:
                assert ours[key] == pytest.approx(row[key][n], abs=1e-9), (
                    row["name"], frame, key,
                )


@pytest.mark.slow
@pytest.mark.parametrize("fps", [24, 30, 60])
def test_python_and_the_renderer_agree_on_the_scene_window(fps: int, tmp_path: Path):
    """`stage_at` и `stageAt` дают одно окно на каждом кадре каждой раскладки."""
    sheet = Visuals(
        scenes=[
            Scene(layout="paper", word_from=9, word_to=17),
            Scene(layout="card_top", word_from=40, word_to=48),
            Scene(layout="phone", word_from=78, word_to=86),
        ]
    )
    plan = dressed_plan(
        build_plan, talk(), "video.mp4", visuals=sheet, apply_cuts=False, cfg=cfg(), fps=fps
    )
    scenes = plan.meta["visuals"]["scenes"]
    assert [s["layout"] for s in scenes] == ["paper", "card_top", "phone"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False), encoding="utf-8")
    assert_same_stage(probe_scenes(path), scenes)


@pytest.mark.slow
def test_the_scene_window_agrees_on_awkward_numbers(tmp_path: Path):
    """Дробные секунды, разные вход и выход, наклон и сдвиг — те же числа у обоих."""
    scenes = [
        PlanScene(
            layout="paper",
            start=2.042,  # 49 кадров при 24 fps, записано с точностью плана
            end=5.292,
            in_frames=9,
            out_frames=17,
            window=Rect(443.37, 274.5, 561.61, 786.13),
            radius=43.0,
            tilt=-3.0,
            scale=0.5204,
            tx=-0.02,
            ty=-103.22,
            bg=PlanSceneBg(kind="paper"),
        ).to_dict(),
        PlanScene(
            layout="phone",
            start=7.0,
            end=9.5,
            in_frames=8,
            out_frames=8,
            window=Rect(248.4, 486.0, 583.2, 1037.0),
            radius=49.0,
            tilt=0.0,
            scale=0.5401,
            tx=-0.05,
            ty=0.0,
            bg=PlanSceneBg(kind="still"),
            chrome="phone",
            bezel=17.0,
        ).to_dict(),
    ]
    plan = {
        "fps": 24,
        "width": 1080,
        "height": 1920,
        "meta": {"visuals": {"version": 1, "overlays": [], "scenes": scenes}},
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    assert_same_stage(probe_scenes(path), scenes)
