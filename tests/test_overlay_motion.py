"""Движение графики — числами, а не на глаз: плашки, стикеры, декор и сцены.

Кривые живут в `render/src/visuals/motion.mjs`, и проверять их моделью на
стороне Python бессмысленно — модель проверяла бы саму себя. Поэтому здесь
запускается пробник `render/overlay_probe.mjs`: он импортирует ТОТ ЖЕ модуль,
которым рисуют кадр `Stage.tsx` и `Overlays.tsx`, и печатает его ответы по
кадрам. Копии кривых в пробнике нет (в отличие от `card_probe.mjs`), так что
числа здесь — это числа ролика.

Что закреплено и зачем:

- сцена — формулы контракта из шапки `src/core/overlay_models.py`. По ним
  Python пересчитывает трек лица, и окно в кадре обязано стоять ровно там, где
  он его посчитал;
- элемент — требования из той же шапки: анимация не короче восьми кадров,
  перелёт масштаба не больше 4 % и ровно один разворот, покой ровно 1.0,
  прозрачность монотонна, вне [start, end] не видно ничего.

Последнее — не украшение: выход обязан КОНЧАТЬСЯ в `end`, а не начинаться в
нём, иначе элемент доживал бы свой хвост поверх следующего приёма, а потолки
плотности листа считаются именно по [start, end].
"""

from __future__ import annotations

import functools
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.core.overlay_models import MIN_ANIM_FRAMES, smoothstep

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "render"
PROBE = RENDER / "overlay_probe.mjs"
VISUALS = RENDER / "src" / "visuals"
MOTION = VISUALS / "motion.mjs"
FIXTURE = REPO / "samples" / "plan-visuals.json"

FPS = [24, 30, 60]
# Перелёт масштаба: больше — уже не подлёт, а прыжок (тот же потолок у карточки
# b-roll в `test_motion.py`).
OVERSHOOT_MAX = 0.04
Row = dict[str, Any]


def _run(*args: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("нет node")
    done = subprocess.run(
        [node, str(PROBE), *args, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=str(RENDER),
    )
    if done.returncode != 0:
        pytest.fail(f"{PROBE.name} упал: {done.stderr.strip()[-2000:]}")
    return done.stdout


@functools.lru_cache(maxsize=None)
def _report(*args: str) -> dict[str, Any]:
    return json.loads(_run(*args))


def bench(fps: int) -> dict[str, Any]:
    """Стенд пробника: по элементу на каждую кривую, декор, карта и три сцены."""
    return _report(str(fps))


def fixture() -> dict[str, Any]:
    """Графика образцового плана — та, на которой рендерер смотрят глазами."""
    return _report("--plan", str(FIXTURE))


# --------------------------------------------------------------------------- помощники


def at(row: Row, key: str, frame: int) -> Any:
    return row[key][frame - row["first_frame"]]


def span(row: Row, key: str, first: int, last: int) -> list[Any]:
    """Значения ряда на кадрах `first..last` включительно."""
    start = row["first_frame"]
    return row[key][first - start : last - start + 1]


def frames_of(row: Row) -> range:
    """Кадры, которые снял пробник: от `PAD` до начала и до `PAD` после конца."""
    series = row["p"] if "p" in row else row["opacity"]
    return range(row["first_frame"], row["first_frame"] + len(series))


def begin(row: Row) -> int:
    """Кадр, от которого идёт вход: у знака декора — со своей задержкой."""
    return row["f0"] + row.get("delay", 0)


def rest(row: Row) -> range:
    """Кадры покоя: вход закончился, выход ещё не начался. Может быть пустым."""
    return range(begin(row) + row["in_frames"], row["f1"] - row["out_frames"] + 1)


def full(row: Row) -> float:
    """Прозрачность в покое: у знака декора она своя (`mark.opacity`)."""
    return row.get("base_opacity", 1.0)


def settled(row: Row, frame: int) -> bool:
    """Стоит ли элемент на этом кадре в покое — весь, а не одной прозрачностью.

    У выезда альфа доезжает раньше сдвига, у «pop» — раньше масштаба: мерить
    длину анимации по одному параметру значило бы принять её хвост за покой.
    Покачивание стикера и снос знака декора покоем и являются — их не считаем.
    """
    if at(row, "opacity", frame) != full(row) or at(row, "scale", frame) != 1.0:
        return False
    if "base_opacity" in row or row.get("float_px", 0) > 0:
        return True
    still = (at(row, "dx", frame), at(row, "dy", frame), at(row, "rotate", frame))
    return still == (0, 0, row["rest_rotate"])


def landing(pin: Row) -> int:
    """Кадр, с которого метка карты стоит на месте (после падения и отскока).

    Не «первый кадр с dy == 0»: в момент касания перед отскоком сдвиг тоже
    ноль, если касание попало ровно на кадр.
    """
    flying = [f for f in frames_of(pin) if at(pin, "visible", f) and at(pin, "dy", f) != 0]
    return flying[-1] + 1


def turns(values: list[float]) -> int:
    """Сколько раз ряд менял направление. У подлёта — один, у пружины — много."""
    steps = [b - a for a, b in zip(values, values[1:]) if abs(b - a) > 1e-9]
    signs = [math.copysign(1, s) for s in steps]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def named(report: dict[str, Any], name: str) -> Row:
    return next(row for row in report["overlays"] if row["name"] == name)


def animated(report: dict[str, Any]) -> list[Row]:
    """Всё, что входит и выходит: элементы и знаки декора."""
    return [*report["overlays"], *report["marks"]]


# --------------------------------------------------------------------------- законы
#
# Каждый закон — функция от отчёта пробника: один и тот же код судит и стенд на
# трёх fps, и образцовый план. Разведи их — и план мог бы нарушать то, что
# стенд соблюдает.


def check_nothing_outside(report: dict[str, Any]) -> None:
    for row in [*animated(report), *report["pins"]]:
        outside = [f for f in frames_of(row) if f < row["f0"] or f > row["f1"]]
        assert outside, f"{row['name']}: пробник не снял кадров вне окна"
        for frame in [*outside, row["f0"], row["f1"]]:
            assert at(row, "visible", frame) is False, f"{row['name']}: виден на кадре {frame}"
            assert at(row, "opacity", frame) == 0, f"{row['name']}: альфа на кадре {frame}"


def check_exit_ends_at_end(report: dict[str, Any]) -> None:
    for row in animated(report):
        name, f1 = row["name"], row["f1"]
        assert at(row, "opacity", begin(row) + 1) > 0, f"{name}: не появился после start"
        assert at(row, "opacity", f1 - 1) > 0, f"{name}: исчез раньше end"
        assert at(row, "opacity", f1) == 0, f"{name}: ещё виден в end"
        if len(rest(row)) > 0:
            # Выход занимает ровно `out_frames` последних кадров, не больше.
            assert at(row, "opacity", f1 - row["out_frames"]) == full(row), (
                f"{name}: выход начался раньше, чем за {row['out_frames']} кадров до end"
            )
            assert at(row, "opacity", f1 - row["out_frames"] + 1) < full(row), (
                f"{name}: выход начался позже, чем за {row['out_frames']} кадров до end"
            )


def check_long_enough(report: dict[str, Any]) -> None:
    for row in animated(report):
        name = row["name"]
        assert row["in_frames"] >= MIN_ANIM_FRAMES and row["out_frames"] >= MIN_ANIM_FRAMES, name
        if len(rest(row)) == 0:
            continue
        calm = [f for f in range(begin(row), row["f1"] + 1) if settled(row, f)]
        assert calm, f"{name}: так и не встал на место"
        entry, leave = calm[0] - begin(row), row["f1"] - calm[-1]
        assert entry >= MIN_ANIM_FRAMES, f"{name}: вход {entry} кадров"
        assert leave >= MIN_ANIM_FRAMES, f"{name}: выход {leave} кадров"
        # И не длиннее заказанного: вход обязан уложиться в своё окно.
        assert entry == row["in_frames"] and leave == row["out_frames"], (
            f"{name}: вход {entry} из {row['in_frames']}, выход {leave} из {row['out_frames']}"
        )
        # Восемь кадров формально, а альфа одним кадром — тот же щелчок.
        alpha = span(row, "opacity", row["f0"], row["f1"])
        step = max(abs(b - a) for a, b in zip(alpha, alpha[1:]))
        assert step <= 0.5 * full(row), f"{name}: кадр берёт {step:.2f} альфы разом"
    for pin in report["pins"]:
        seen = [f for f in frames_of(pin) if at(pin, "visible", f)]
        assert landing(pin) - (seen[0] - 1) >= MIN_ANIM_FRAMES, f"{pin['name']}: метка упала за миг"
    for scene in report["scenes"]:
        moving = [f for f in frames_of(scene) if 0 < at(scene, "p", f) < 1]
        entry = [f for f in moving if f < scene["f0"] + scene["in_frames"]]
        leave = [f for f in moving if f > scene["f1"] - scene["out_frames"]]
        # Кадров строго между 0 и 1 на один меньше длины перехода.
        assert len(entry) + 1 >= MIN_ANIM_FRAMES, f"{scene['name']}: вход {len(entry) + 1} кадров"
        assert len(leave) + 1 >= MIN_ANIM_FRAMES, f"{scene['name']}: выход {len(leave) + 1} кадров"


def check_scale(report: dict[str, Any]) -> None:
    for row in report["overlays"]:
        name = row["name"]
        scale = span(row, "scale", row["f0"] + 1, row["f1"] - 1)
        peak = max(scale)
        assert peak - 1 <= OVERSHOOT_MAX + 1e-9, f"{name}: перелёт {(peak - 1) * 100:.2f} %"
        for frame in rest(row):
            assert at(row, "scale", frame) == 1.0, f"{name}: масштаб покоя на кадре {frame}"
            assert at(row, "opacity", frame) == 1.0, f"{name}: альфа покоя на кадре {frame}"
        if row["anim"] != "pop":
            assert all(v == 1.0 for v in scale), f"{name}: `{row['anim']}` трогает масштаб"
            continue
        if len(rest(row)) == 0:
            continue
        assert peak > 1.005, f"{name}: перелёта нет — pop приезжает по рельсам"
        assert turns(scale) == 1, f"{name}: разворотов {turns(scale)} — это колебание"
        assert scale[0] < 0.8, f"{name}: pop стартует с {scale[0]:.2f}, а не от ~0.6"
    for mark in report["marks"]:
        scale = span(mark, "scale", begin(mark) + 1, mark["f1"] - 1)
        assert max(scale) <= 1.0, f"{mark['name']}: знак перелетел"
        assert turns(scale) <= 1, f"{mark['name']}: масштаб знака колеблется"
        for frame in rest(mark):
            assert at(mark, "scale", frame) == 1.0, f"{mark['name']}: масштаб покоя"


def check_opacity_monotonic(report: dict[str, Any]) -> None:
    for row in animated(report):
        name, top = row["name"], full(row)
        alpha = span(row, "opacity", row["f0"], row["f1"])
        assert all(0 <= v <= top + 1e-12 for v in alpha), f"{name}: альфа вышла за 0..{top}"
        if len(rest(row)) == 0:
            # Вход и выход наложились: один подъём и один спад, без второго горба.
            assert turns(alpha) == 1, f"{name}: альфа моргает"
            continue
        enter = span(row, "opacity", row["f0"], begin(row) + row["in_frames"])
        leave = span(row, "opacity", row["f1"] - row["out_frames"], row["f1"])
        assert all(b >= a for a, b in zip(enter, enter[1:])), f"{name}: альфа падает на входе"
        assert all(b <= a for a, b in zip(leave, leave[1:])), f"{name}: альфа растёт на выходе"
        assert all(at(row, "opacity", f) == top for f in rest(row)), f"{name}: альфа в покое"


def check_stage(report: dict[str, Any]) -> None:
    width, height = report["width"], report["height"]
    neutral = dict(x=0, y=0, width=width, height=height, radius=0, tilt=0, scale=1, tx=0, ty=0)
    for index, scene in enumerate(report["scenes"]):
        name, f0, f1, target = scene["name"], scene["f0"], scene["f1"], scene["rest"]
        assert at(scene, "p", f0) == 0, f"{name}: p в начале сцены"
        assert at(scene, "p", f1) == 0, f"{name}: p в конце сцены — выход обязан кончиться в end"
        assert at(scene, "p", f0 + 1) > 0 and at(scene, "p", f1 - 1) > 0, f"{name}: сцена короче"
        for frame in frames_of(scene):
            p = at(scene, "p", frame)
            # Формула контракта, посчитанная его же `smoothstep`.
            if frame <= f0 or frame >= f1:
                expected = 0.0
            else:
                expected = min(
                    smoothstep((frame - f0) / scene["in_frames"]),
                    smoothstep((f1 - frame) / scene["out_frames"]),
                )
            assert p == pytest.approx(expected, abs=1e-12), f"{name}: p на кадре {frame}"
            want = {
                "x": p * target["x"],
                "y": p * target["y"],
                "width": width + p * (target["width"] - width),
                "height": height + p * (target["height"] - height),
                "radius": p * target["radius"],
                "tilt": p * target["tilt"],
                "scale": 1 + p * (target["scale"] - 1),
                "tx": p * target["tx"],
                "ty": p * target["ty"],
            }
            for key, value in want.items():
                assert at(scene, key, frame) == pytest.approx(value, abs=1e-9), (
                    f"{name}: {key} на кадре {frame}"
                )
            if frame < f0 or frame > f1:
                # Вне сцены — окно во весь кадр: дерево то же, стили нейтральные.
                assert {k: at(scene, k, frame) for k in neutral} == neutral, f"{name}: {frame}"
                assert at(scene, "active", frame) != index
            else:
                assert at(scene, "active", frame) == index, f"{name}: чужая сцена на кадре {frame}"
        still = range(f0 + scene["in_frames"], f1 - scene["out_frames"] + 1)
        assert len(still) > 0, f"{name}: у сцены нет покоя"
        for frame in still:
            assert at(scene, "p", frame) == 1.0, f"{name}: p в покое на кадре {frame}"
            # В покое — числа плана как есть, без хвостов округления.
            for key, value in target.items():
                assert at(scene, key, frame) == value, f"{name}: {key} в покое"


LAWS = [
    check_nothing_outside,
    check_exit_ends_at_end,
    check_long_enough,
    check_scale,
    check_opacity_monotonic,
    check_stage,
]


# --------------------------------------------------------------------------- стенд


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
@pytest.mark.parametrize("law", LAWS, ids=lambda law: law.__name__)
def test_bench_obeys_the_motion_laws(law: Any, fps: int):
    """Все требования контракта — на стенде, на 24, 30 и 60 fps.

    Три fps, потому что длительности округляются до кадров: кривая, гладкая на
    30, на 24 может потерять кадр на вершине, а на 60 — обрести лишний разворот.
    """
    report = bench(fps)
    assert report["min_anim_frames"] == MIN_ANIM_FRAMES
    assert report["overlays"] and report["marks"] and report["pins"] and report["scenes"]
    law(report)


@pytest.mark.slow
@pytest.mark.parametrize("law", LAWS, ids=lambda law: law.__name__)
def test_sample_plan_obeys_the_motion_laws(law: Any):
    """Те же законы — на графике образцового плана `samples/plan-visuals.json`.

    В нём все виды элементов и все три сцены с настоящими числами, а не
    стендовыми: именно этот план рендерят, когда проверяют вид глазами.
    """
    report = fixture()
    assert len(report["scenes"]) == 3, "в образцовом плане должны быть все три сцены"
    assert {row["kind"] for row in report["overlays"]} >= {"chip", "sticker", "photo", "map"}
    assert report["marks"], "в образцовом плане нет декора"
    law(report)


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_animation_shorter_than_eight_frames_is_stretched_to_eight(fps: int):
    """План попросил три кадра входа и два выхода — получил по восемь.

    Длительность из плана — пожелание, восемь кадров — закон (как `animFrames`
    в `style.ts`): короче переход читается как мигание.
    """
    row = named(bench(fps), "asked_too_short")
    assert (row["asked_in"], row["asked_out"]) == (3, 2)
    assert (row["in_frames"], row["out_frames"]) == (MIN_ANIM_FRAMES, MIN_ANIM_FRAMES)
    assert at(row, "scale", row["f0"] + 3) != 1.0, "вход закончился за три кадра"
    assert at(row, "opacity", row["f1"] - 3) < 1.0, "выход уложился в два кадра"


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_slide_rises_into_place_and_fade_does_not_move(fps: int):
    """`slide` выезжает снизу без масштаба, `fade` — одна прозрачность."""
    report = bench(fps)
    slide, fade = named(report, "slide"), named(report, "fade")
    enter = span(slide, "dy", slide["f0"] + 1, slide["f0"] + slide["in_frames"])
    assert enter[0] > 10, f"выезд начинается с {enter[0]:.1f} px — его не видно"
    assert all(b <= a for a, b in zip(enter, enter[1:])), "выезд идёт назад"
    assert all(at(slide, "dy", f) == 0 for f in rest(slide)), "после выезда элемент не на месте"
    leave = span(slide, "dy", slide["f1"] - slide["out_frames"] + 1, slide["f1"] - 1)
    assert all(v < 0 for v in leave), "уходя, `slide` обязан ехать дальше вверх"
    for key in ("dx", "dy"):
        assert all(v == 0 for v in fade[key]), f"`fade` двигает элемент по {key}"
    assert all(v == fade["rest_rotate"] for v in fade["rotate"]), "`fade` вращает элемент"


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_resting_element_stands_exactly_in_its_place(fps: int):
    """В покое элемент без покачивания стоит в прямоугольнике плана, пиксель в пиксель.

    Python разводит графику с субтитрами и лицом по прямоугольникам покоя;
    остаточный сдвиг или недокрученный угол он не видит.
    """
    for row in bench(fps)["overlays"]:
        if row["float_px"] > 0:
            continue
        for frame in rest(row):
            state = {k: at(row, k, frame) for k in ("dx", "dy", "rotate")}
            assert state == {"dx": 0, "dy": 0, "rotate": row["rest_rotate"]}, (
                f"{row['name']}: кадр {frame}, {state}"
            )


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_card_and_sticker_turn_into_place_and_a_straight_chip_does_not(fps: int):
    """Стикер и карточка доворачиваются до своего наклона; ровная плашка не крутится."""
    report = bench(fps)
    for name, least in (("photo", 3.0), ("map", 3.0), ("pop_tilted", 2.0), ("sticker_float", 8.0)):
        row = named(report, name)
        off = [
            abs(v - row["rest_rotate"])
            for v in span(row, "rotate", row["f0"] + 1, row["f0"] + row["in_frames"])
        ]
        assert off[0] >= least, f"{name}: довёрнут на {off[0]:.1f}° — вращения не видно"
        if row["float_px"] == 0:
            assert all(b <= a for a, b in zip(off, off[1:])), f"{name}: угол качается на входе"
            assert off[-1] == 0, f"{name}: не докрутился до наклона покоя"
    straight = named(report, "pop_straight")
    assert all(v == 0 for v in straight["rotate"]), "ровная плашка вращается на входе"


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_idle_float_is_slow_and_stays_within_its_amplitude(fps: int):
    """Покачивание стикера: не дальше `float_px`, медленно и на самом деле есть."""
    report = bench(fps)
    row = named(report, "sticker_float")
    amplitude, period = row["float_px"], report["constants"]["FLOAT_PERIOD_S"]
    still = list(rest(row))
    lift = [at(row, "dy", f) for f in still]
    assert max(abs(v) for v in lift) <= amplitude + 1e-9, "стикер ушёл дальше своей амплитуды"
    assert max(lift) - min(lift) >= amplitude, "стикер в покое стоит как вкопанный"
    # Самый быстрый кадр синусоиды — в нуле: 2π·A / (период в кадрах).
    fastest = 2 * math.pi * amplitude / (period * fps)
    assert max(abs(b - a) for a, b in zip(lift, lift[1:])) <= fastest * 1.01, "стикер дрожит"
    sway = report["constants"]["FLOAT_SWAY_DEG"]
    assert all(abs(at(row, "rotate", f) - row["rest_rotate"]) <= sway + 1e-9 for f in still)
    # Плечо по горизонтали короче вертикального: стикер качается, а не плавает кругами.
    assert max(abs(at(row, "dx", f)) for f in still) <= amplitude * 0.35 + 1e-9


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_decor_marks_come_in_one_by_one_and_drift(fps: int):
    """Знаки декора проступают по очереди, успевают до общего ухода и плывут."""
    report = bench(fps)
    marks = sorted(report["marks"], key=lambda m: m["index"])
    assert len(marks) == 6
    first_seen = [next(f for f in frames_of(m) if at(m, "visible", f)) for m in marks]
    assert first_seen == sorted(set(first_seen)), f"знаки вошли не по очереди: {first_seen}"
    for mark in marks:
        name = mark["name"]
        assert len(rest(mark)) > 0, f"{name}: не успел войти до общего ухода"
        assert max(mark["opacity"]) == mark["base_opacity"], f"{name}: не набрал свою альфу"
        still = list(rest(mark))
        if len(still) > 1:
            a, b = still[0], still[-1]
            moved = math.hypot(
                at(mark, "dx", b) - at(mark, "dx", a), at(mark, "dy", b) - at(mark, "dy", a)
            )
            assert moved > 0.5, f"{name}: знак в покое не двигается"
        # Плывёт, но не уплывает: за всю жизнь — не дальше трети своего кегля.
        reach = max(
            math.hypot(at(mark, "dx", f), at(mark, "dy", f))
            for f in range(mark["f0"] + 1, mark["f1"])
        )
        assert reach <= mark["size"] / 3, f"{name}: уплыл на {reach:.0f} px из {mark['size']}"
    # Не в унисон: в один и тот же кадр знаки стоят в разных фазах качания.
    frame = marks[-1]["f1"] - marks[-1]["out_frames"]
    tilt = {round(at(m, "rotate", frame) - m["rest_rotate"], 3) for m in marks}
    assert len(tilt) == len(marks), "знаки декора качаются в унисон"


@pytest.mark.slow
@pytest.mark.parametrize("fps", FPS)
def test_map_pin_drops_after_the_card_has_landed(fps: int):
    """Метка падает на карту, когда карточка уже стоит, и приземляется в свою точку."""
    report = bench(fps)
    pin = report["pins"][0]
    seen = [f for f in frames_of(pin) if at(pin, "visible", f)]
    assert seen[0] > pin["f0"] + pin["in_frames"], "метка падает на ещё летящую карточку"
    fall = [at(pin, "dy", f) for f in seen[:4]]
    assert fall[0] < -0.3 * named(report, "map")["height"], "метка появляется почти на месте"
    steps = [b - a for a, b in zip(fall, fall[1:])]
    assert all(b > a > 0 for a, b in zip(steps, steps[1:])), "метка не разгоняется к земле"
    landed = landing(pin)
    assert landed <= pin["f1"] - pin["out_frames"], "метка приземлилась уже на выходе карточки"
    for frame in range(landed, pin["f1"]):
        assert at(pin, "dy", frame) == 0 and at(pin, "shadow", frame) == 1, f"кадр {frame}"
        assert at(pin, "opacity", frame) == 1


@pytest.mark.slow
def test_python_mirror_of_the_stage_agrees_with_the_renderer():
    """`overlays.stage_at` и `stageAt` в рендерере — одна формула в двух языках.

    Python по ней пересчитывает трек лица во время сцены. Разойдись копии —
    субтитр, привязанный к голове, встал бы мимо головы, и ни один из двух
    наборов тестов по отдельности этого бы не заметил.
    """
    overlays = pytest.importorskip("src.core.overlays")
    stage_at = getattr(overlays, "stage_at", None)
    if stage_at is None:
        pytest.skip("в src.core.overlays ещё нет stage_at")
    report = fixture()
    plan = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenes = plan["meta"]["visuals"]["scenes"]
    assert len(scenes) == len(report["scenes"])
    for scene, row in zip(scenes, report["scenes"]):
        for frame in frames_of(row):
            mirror = stage_at(scene, frame, report["fps"], report["width"], report["height"])
            for key, value in mirror.items():
                assert at(row, key, frame) == pytest.approx(value, abs=1e-9), (
                    f"{row['name']}: {key} на кадре {frame}"
                )


@pytest.mark.slow
def test_probe_is_deterministic():
    """Два прогона дают один и тот же вывод, байт в байт: ни случайности, ни часов.

    Перерендер сохранённого листа обязан дать то же видео; зерно — единственный
    источник «случайного», и одно зерно даёт одну последовательность.
    """
    assert _run("30") == _run("30")
    assert _run("--plan", str(FIXTURE)) == _run("--plan", str(FIXTURE))
    numbers = bench(30)["prng"]
    for seed, values in numbers.items():
        assert all(0 <= v < 1 for v in values), f"зерно {seed}: число вне 0..1"
        assert len(set(values)) == len(values), f"зерно {seed}: числа повторяются"
    assert numbers["7"] != numbers["8"] != numbers["0"], "разные зёрна дали одну последовательность"


# --------------------------------------------------------------------------- без node
#
# Текстовые проверки исходников: исполнить TSX вне композиции нечем, а правила
# слоя — запреты, и нарушение запрета видно по тексту.


def _code(path: Path) -> str:
    """Исходник без комментариев: слова «filter» и «random» в них — не код."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[\s;,{(])//.*$", "", text)


def test_visuals_layer_has_no_randomness_and_no_clocks():
    """Ни `Math.random`, ни `Date`: кадр зависит только от плана и номера кадра."""
    for name in ("motion.mjs", "Stage.tsx", "Overlays.tsx", "DevReel.tsx", "dev-entry.ts"):
        code = _code(VISUALS / name)
        for banned in ("Math.random", "Date.now", "new Date", "performance.now"):
            assert banned not in code, f"{name}: {banned} — перерендер даст другое видео"


def test_visuals_layer_animates_without_filters_and_second_video():
    """Бюджет рендера: без `filter` и `backdrop-filter`, без второй копии видео.

    Ролик собирается на процессоре с программным GL: размытие на каждом кадре
    там дороже всей остальной графики, а второе видео — второе декодирование.
    """
    for name in ("Stage.tsx", "Overlays.tsx"):
        code = _code(VISUALS / name)
        assert not re.search(r"backdrop-?filter", code, re.I), f"{name}: backdrop-filter"
        assert not re.search(r"\bfilter\s*:", code), f"{name}: CSS filter"
        assert not re.search(r"<(Video|OffthreadVideo)\b", code), f"{name}: вторая копия видео"
        assert "transition" not in code and "@keyframes" not in code, f"{name}: CSS-анимация"


def test_motion_module_is_pure_and_shares_the_animation_floor():
    """`motion.mjs` — без импортов (его грузит и бандл, и голый node), порог — как в Python."""
    code = _code(MOTION)
    assert not re.search(r"(?m)^\s*import\b|\brequire\(", code), "motion.mjs что-то импортирует"
    floor = re.search(r"export const MIN_ANIM_FRAMES = (\d+);", code)
    assert floor, "в motion.mjs нет MIN_ANIM_FRAMES"
    assert int(floor.group(1)) == MIN_ANIM_FRAMES, "порог анимации разошёлся с overlay_models"
    for name in ("smoothstep", "stageAt", "overlayAt", "markAt", "prng"):
        assert re.search(rf"export const {name} = ", code), f"motion.mjs не экспортирует {name}"
