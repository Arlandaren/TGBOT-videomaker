"""Графика поверх ролика: лист (`Visuals`) и готовый план → `plan.meta["visuals"]`.

Контракт — `src/core/overlay_models.py`. Лист говорит, ЧТО показать и на каком
слове; здесь решается всё остальное: когда именно (секунды на сетке кадров), где
(прямоугольник в пикселях), каким кеглем и цветом. Рендерер ничего из этого не
выводит (§0) — ему разрешены только `transform` и `opacity` на входе и выходе.

Три правила, на которых держится модуль.

1. ГРАФИКА УСТУПАЕТ, А НЕ ТЕСНИТ. Субтитр, вставка, титр и лицо стоят там, где
   их поставил `build_plan`; элемент ищет себе место среди них и, не найдя,
   выпадает. Обратного не бывает никогда: плашка, сдвинувшая субтитр, ломает
   гарантию Ф7 («картинка не пересекает текст»), которую план уже дал.
   Единственное, что графика меняет в плане, — блоки ВНУТРИ сцены: там кадр
   перекроен целиком, и субтитр обязан стоять внизу плакатом. Это делается так
   же, как режим «Два видео» (`render_plan`, ветка `split_until`): блоки
   переписываются на копии листа, которая никуда не сохраняется.

2. НИЧЕГО НЕ ПАДАЕТ. Перебор с приёмом — не ошибка формы: сцена поверх
   вставки, плашка, которой нет места, стикер с незнакомым ключом молча
   выпадают, ролик выходит без них (так же устроены список и привязка к голове).

3. ПЕРЕРЕНДЕР ДАЁТ ТО ЖЕ ВИДЕО (инвариант 4). Никаких часов и никакого
   `random` без зерна: зерно элемента — его место в списке листа.

Четыре входа снаружи: `dressed_plan` (шов в `camera.filmed_plan`), `stage_at`
(зеркало формул движения сцены), `sfx_events` (шов в `audio_post.plan_sfx`) и
`stage_visual_assets` (шов в `render.prepare_assets`).
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import logging
import math
import os
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Sequence

from pydantic import ValidationError

from src.core.config import REPO_ROOT, Settings, settings as default_settings
from src.core.edl import EDL, Timeline, list_runs
from src.core.geometry import (
    Rect,
    caption_zone,
    free_band,
    line_box_height,
    measure_line,
    metrics_for,
    safe_margin_x,
    safe_margin_y,
    single_emoji,
)
from src.core.overlay_models import (
    MIN_ANIM_FRAMES,
    Overlay,
    PlanMark,
    PlanOverlay,
    PlanScene,
    PlanSceneBg,
    PlanVisuals,
    Scene,
    SceneItem,
    Visuals,
    WordWindow,
    overlay_window,
    scene_window,
    smoothstep,
)
from src.core.preset import Preset, hex_to_rgb, load_preset
from src.core.render_plan import (
    FACE_GUARD_DOWN,
    FACE_GUARD_UP,
    FACE_MIN_CONF,
    FACE_WIDTH_RATIO,
    PlanBlock,
    RenderPlan,
)
from src.core.track import FaceTrack

log = logging.getLogger(__name__)

__all__ = [
    "dressed_plan",
    "sfx_events",
    "stage_at",
    "stage_visual_assets",
]

# Набор стикеров. Оглавление читается при обращении, а не при импорте модуля:
# набор пополняют отдельным инструментом, и воркер не должен перезапускаться
# ради новой картинки.
STICKERS_DIR = REPO_ROOT / "assets" / "stickers"

# --------------------------------------------------------------------------- время

# Допуск сравнения секунд — тот же, что у правил листа (см. `_overlap`).
TIME_EPS = 1e-6

# Длительности анимаций. Секунды, а не кадры: 0.4 с — это одно и то же движение
# при 24 и при 60 fps. В кадры они переводятся по месту, с полом
# `MIN_ANIM_FRAMES` (короче восьми кадров анимация читается как дефект) и
# потолком в треть жизни элемента — иначе вход перетекает в выход, и элемент ни
# кадра не стоит на месте.
POP_IN_S = 0.4
POP_OUT_S = 0.3
FADE_IN_S = 0.5
FADE_OUT_S = 0.4
SCENE_IN_S = 0.5
SCENE_OUT_S = 0.5
# Сколько содержимое сцены обязано простоять. Пункт, привязанный к последнему
# слову сцены, по букве правила «входит на своём слове» появился бы уже после
# того, как доска уехала; такой пункт входит раньше слова, а не пропадает.
ITEM_MIN_LIFE_S = 1.2
# Пункты не приходят в один кадр: одновременный вход трёх карточек читается как
# смена картинки, а не как раскладывание по доске.
ITEM_STAGGER_S = 0.12

# --------------------------------------------------------------------------- звук
#
# Имена файлов из `assets/sfx` (без расширения). Строками, а не импортом из
# `pipeline.audio_post`: ядро не тянет за собой конвейер. Что такие файлы есть,
# сторожит тест.
SFX_SCENE_IN = "whoosh"
SFX_SCENE_OUT = "swish"
SFX_OVERLAY = {"chip": "tick", "sticker": "swish", "decor": "swish"}

# --------------------------------------------------------------------------- лицо
#
# Где лицо, когда трека нет (кроп его не нашёл) или в окне элемента оно
# потеряно: чуть выше центра кадра — там стоит говорящая голова
# (`camera.DEFAULT_ORIGIN`). Гадать приходится, потому что цена ошибок разная:
# лишний раз обойти пустое место ничего не стоит, а плашка на лице — брак.
NOMINAL_FACE = (0.5, 0.42, 0.22)  # центр по X и Y, высота — доли кадра
# Макушка: бокс детектора идёт от бровей до подбородка, волосы и лоб выше него.
HEAD_UP = 0.95
# Край головы сбоку — от центра лица, в высотах лица (уши и причёска).
HEAD_SIDE = 0.7

# --------------------------------------------------------------------------- плашки

CHIP_LINE_HEIGHT = 1.1
# Просвет между значком и текстом плашки, когда тема его не задала.
CHIP_EMOJI_GAP_EM = 0.24
# У шрифта темы может не быть знака: тогда его рисует системный фолбэк, ширину
# которого мы не знаем (`geometry.FALLBACK_ADVANCE_EM` — оценка, а не замер), и
# коробка плашки расходится с кадром. Цена на экране без рубля встречается чаще
# всего, поэтому ему подставляется слово, которое шрифт набрать умеет.
GLYPH_FALLBACKS = {"₽": " руб.", "−": "-"}

INK_DARK = "#111111"
INK_LIGHT = "#FFFFFF"
TAG_FILL = "#FFFFFF"


@dataclass(frozen=True, slots=True)
class ChipStyle:
    """Числа одного вида плашки. Кегль — пиксели при высоте кадра 1920."""

    size: float
    pad_x: float  # поля, em
    pad_y: float
    radius: float | None  # em; `None` — капсула, половина высоты
    upper: bool = False
    # До какой доли кегля плашку можно ужать, чтобы влезла. Ниже — выпадает:
    # мелкая плашка перестаёт быть приёмом и читается как служебная надпись.
    floor: float = 0.7


CHIP_STYLES: dict[str, ChipStyle] = {
    "pill": ChipStyle(58, 0.62, 0.3, None),
    "tag": ChipStyle(56, 0.6, 0.32, 0.34),
    "label": ChipStyle(38, 0.62, 0.34, 0.3, upper=True),
    # Заголовок сцены: текст без коробки, поэтому полей нет, а ужимать его
    # можно сильнее — тридцать два знака кеглем 92 шире кадра.
    "heading": ChipStyle(92, 0.0, 0.0, 0.0, upper=True, floor=0.45),
}

# --------------------------------------------------------------------------- стикеры
#
# Цветной эмодзи-шрифт растровый: его картинки нарисованы под кегль ~109, и
# выше 140 они заметно мылятся. Поэтому у глифа потолок в пикселях, а не в
# долях кадра.
GLYPH_MAX_PX = 140
GLYPH_RATIO = 0.073  # кегль глифа, доля высоты кадра
# Картинка глифа шире и выше своего кегля (136×128 точек на кегль 109): место
# под него считается по картинке, иначе её край вылезает из прямоугольника.
GLYPH_BOX = (1.25, 1.18)
STICKER_SIDE = 0.115  # большая сторона стикера-картинки, доля высоты кадра
# На доске сцены стикер — содержимое, а не пометка на полях: там он крупнее.
SCENE_STICKER_SIDE = 0.15
# Стикеру тесно в слоте — он пробует встать туда же мельче, прежде чем искать
# другое место: «над головой» лист просит не случайно, а запас над макушкой у
# говорящей головы всего в десятую часть кадра.
STICKER_SCALES = (1.0, 0.85, 0.72)
STICKER_FLOAT = 0.005  # амплитуда покачивания, доля высоты кадра
PHOTO_ASPECT = 0.8  # пропорция фото, когда файл не измерен: вставки рисуются 4:5

# Знак декора в кадре живёт: покачивается и всплывает (`motion.mjs`, `markAt`).
# На столько его кегля он уходит вверх за жизнь декора и в стороны при качании.
MARK_RISE = 0.25
MARK_SWAY = 0.06

# --------------------------------------------------------------------------- сцены

PAPER_COLOR = "#F3EFE6"
PAPER_LINE = "#C9D3E0"
PAPER_INK = "#1B2A4A"
PAPER_ACCENT = "#2F6BFF"
BOARD_TOP = "#101522"
BOARD_BOTTOM = "#1B2540"
STILL_TOP = "#0B0D12"
STILL_BOTTOM = "#161A22"
PHONE_BEZEL = "#0B0B0F"
CARD_BORDER = "#FFFFFF"
# Ниже этого контраста с фоном сцены акцент темы на нём не читается (золото на
# кремовой бумаге) — берётся запасной цвет раскладки.
ACCENT_MIN_CONTRAST = 2.2
# Размытый кадр под телефоном: во сколько раз темнее исходного и на каком
# масштабе размывается (мелкая копия — это и есть дешёвое сильное размытие).
STILL_DARKEN = 0.55
STILL_SHRINK = 8
STILL_SIGMA = 3.0


def _round(value: float) -> int:
    """Округление «половина вверх» — как `Math.round` в рендерере.

    Питоновский `round` банковский; на ровной половине кадра Python и рендерер
    выбрали бы соседние кадры (`geometry._round_px` — тот же довод).
    """
    return int(math.floor(value + 0.5))


# --------------------------------------------------------------------------- цвет


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


# Фон светлее этого — субтитр темы (почти белый у всех тем) на нём не читается,
# и блок получает тёмные цвета (`light_ink`). Бумага сцены — ~0.86.
LIGHT_BG_LUMA = 0.5
# Тёмные цвета блока на светлой сцене: чернила бумаги, синий акцент графики,
# белая обводка вместо тёмной (тёмная на тёмном тексте читается как жирность).
LIGHT_INK = {"text": "#14110A", "accent": "#2F6BFF", "outline": "#FFFFFF", "shadow": "#FFFFFF"}


def light_ink(bg_luma: float | None) -> dict[str, str] | None:
    """Цвета блока на светлой сцене или `None` — на тёмной остаются цвета темы."""
    if bg_luma is None or bg_luma < LIGHT_BG_LUMA:
        return None
    return dict(LIGHT_INK)


def luminance(color: str) -> float:
    """Относительная яркость по WCAG — та же единица, что у `bg_luma` (`luma.py`)."""
    r, g, b = (_linear(c / 255) for c in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _ink_on(fill: str) -> str:
    """Цвет текста на заливке: тот из двух, что контрастнее."""
    return INK_DARK if _contrast(INK_DARK, fill) >= _contrast(INK_LIGHT, fill) else INK_LIGHT


def _mix(a: str, b: str, t: float) -> str:
    """Смесь двух цветов в sRGB-кодах — так градиент считает сам Chromium."""
    ca, cb = hex_to_rgb(a), hex_to_rgb(b)
    return "#" + "".join(f"{_round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def _accent_on(background: str, accent: str, spare: str) -> str:
    """Акцент темы, если он читается на этом фоне, иначе запасной цвет раскладки."""
    return accent if _contrast(accent, background) >= ACCENT_MIN_CONTRAST else spare


# --------------------------------------------------------------------------- геометрия


def hit_box(rect: Rect, rotate: float = 0.0, lift: float = 0.0) -> Rect:
    """Место, которое элемент занимает на самом деле, а не в покое.

    Наклонённый ценник шире и выше своего прямоугольника (440×96 под шестью
    градусами занимает 448×142), а стикер ещё и покачивается на `lift` пикселей
    вверх и вниз. Разводить с субтитром надо именно это место: трансформа
    прямоугольник покоя не видит.
    """
    if not rotate and not lift:
        return rect
    angle = math.radians(abs(rotate))
    cos, sin = math.cos(angle), math.sin(angle)
    wide = rect.width * cos + rect.height * sin
    tall = rect.height * cos + rect.width * sin
    return Rect(rect.center_x - wide / 2, rect.center_y - tall / 2 - lift, wide, tall + 2 * lift)


def mark_box(mark: PlanMark | dict[str, Any]) -> Rect:
    """Квадрат, который занимает знак декора: центр и кегль."""
    if isinstance(mark, dict):
        x, y, size = float(mark["x"]), float(mark["y"]), float(mark["size"])
    else:
        x, y, size = mark.x, mark.y, mark.size
    return Rect(x - size / 2, y - size / 2, size, size)


def _union(rects: Sequence[Rect]) -> Rect:
    left = min(r.x for r in rects)
    top = min(r.y for r in rects)
    return Rect(
        left, top, max(r.right for r in rects) - left, max(r.bottom for r in rects) - top
    )


def _centered(box: Rect, width: float, height: float) -> Rect:
    return Rect(box.center_x - width / 2, box.center_y - height / 2, width, height)


def _tilt(seed: int, low: float, high: float) -> float:
    """Наклон из зерна: знак — чётность, величина — три ступени между границами."""
    size = low + (high - low) * ((seed * 7) % 3) / 2
    return round(size if seed % 2 == 0 else -size, 1)


# --------------------------------------------------------------------------- лист → окна времени


def _sheet(visuals: Any) -> Visuals | None:
    """Лист в виде модели. Словарь (сырой JSON листа) принимается как есть.

    Лист, который не проходит по форме, — это ролик без графики, а не упавший
    рендер: графика украшение, и терять из-за неё оплаченный монтаж нельзя.
    Чинить такой лист — дело правил (`overlay_rules`), они стоят раньше.
    """
    if visuals is None or isinstance(visuals, Visuals):
        return visuals
    try:
        return Visuals.model_validate(visuals)
    except ValidationError as err:
        log.warning("лист графики не прочитан (%s) — ролик выйдет без графики", err)
        return None


def _word_windows(edl: EDL, timeline: Timeline) -> WordWindow:
    def window_of(idx: int) -> tuple[float, float] | None:
        word = edl.word_by_idx(idx)
        return timeline.word_window(word) if word is not None else None

    return window_of


def _blockers(
    edl: EDL, window_of: WordWindow, duration: float, preset: Preset, cfg: Settings
) -> list[tuple[float, float, str]]:
    """Промежутки ролика, в которых сцене не место: (начало, конец, причина).

    Считается по ЛИСТУ, а не по собранному плану, намеренно. Блоки сцены
    переписываются до сборки, и вопрос «встанет ли титр» от этого зависит: титр,
    которому мешал верхний блок, после переписи встал бы — прямо под сцену.
    Поэтому занятым считается всё, что МОЖЕТ встать, а не то, что встало.
    """
    out: list[tuple[float, float, str]] = []
    if edl.split_until is not None:
        # Кадр поделён на два видео: окно говорящего там уже есть, и оно не
        # наше. Сцены снимаются во всём ролике — как камера (`camera.plan_camera`).
        out.append((0.0, max(duration, edl.split_until), "split"))
    card = preset.caption.title_card
    if card.enabled and (edl.title or "").strip():
        out.append((card.start, card.start + card.duration, "title"))
    for item in edl.broll:
        window = window_of(item.word_idx)
        if window is not None:
            out.append((window[0], min(window[0] + item.duration, duration), "broll"))
    for run in list_runs(edl.captions):
        spans = [
            window
            for i in run
            for word in edl.captions[i].words
            if (window := window_of(word.word_idx)) is not None
        ]
        if spans:
            out.append((spans[0][0], spans[-1][1] + cfg.sub_tail, "list"))
    return out


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Пересекаются ли два промежутка времени. Касание встык — не пересечение.

    Допуск тот же, что у правил листа (`overlay_rules.EPS`): окна считаются
    сложением чисел с плавающей точкой, и сцена, начавшаяся ровно с концом
    вставки, не должна выпасть из-за хвоста в двенадцатом знаке — иначе план
    обрезал бы лист, который проверка пропустила.
    """
    return a[0] < b[1] - TIME_EPS and b[0] < a[1] - TIME_EPS


def _live_scenes(
    sheet: Visuals,
    window_of: WordWindow,
    duration: float,
    blocked: Iterable[tuple[float, float, str]] = (),
) -> list[tuple[int, Scene, tuple[float, float]]]:
    """Сцены, пережившие правила времени: (место в листе, сцена, окно)."""
    busy = list(blocked)
    out: list[tuple[int, Scene, tuple[float, float]]] = []
    for index, scene in enumerate(sheet.scenes):
        window = scene_window(scene, window_of, duration)
        if window is None:
            log.info("сцена %d (%s) выпала: не укладывается в правила времени", index, scene.layout)
            continue
        why = next((w for a, b, w in busy if _overlap(window, (a, b))), None)
        if why is not None:
            log.info("сцена %d (%s) выпала: её время занято (%s)", index, scene.layout, why)
            continue
        if any(_overlap(window, other) for _, _, other in out):
            log.info("сцена %d (%s) выпала: пересекается с другой сценой", index, scene.layout)
            continue
        out.append((index, scene, window))
    return out


def _live_overlays(
    sheet: Visuals,
    window_of: WordWindow,
    duration: float,
    scenes: Sequence[tuple[int, Scene, tuple[float, float]]],
    split_until: float | None = None,
) -> list[tuple[int, Overlay, tuple[float, float]]]:
    """Свободные элементы, пережившие правила времени.

    Во время сцены в кадре живёт только её содержимое: свободный элемент,
    задевший сцену хоть кадром, выпадает целиком. Пока кадр поделён на два
    видео, места ему тоже нет — трек лица снят с полного кадра, а голова стоит
    в нижней половине (`render_plan`, ветка `split_until`).
    """
    out: list[tuple[int, Overlay, tuple[float, float]]] = []
    for index, overlay in enumerate(sheet.overlays):
        window = overlay_window(overlay, window_of, duration)
        if window is None:
            log.info("элемент %d (%s) выпал: слово вырезано или ролик кончен", index, overlay.kind)
            continue
        if any(_overlap(window, span) for _, _, span in scenes):
            log.info("элемент %d (%s) выпал: в это время идёт сцена", index, overlay.kind)
            continue
        if split_until is not None and window[0] < split_until:
            log.info("элемент %d (%s) выпал: кадр поделён на два видео", index, overlay.kind)
            continue
        out.append((index, overlay, window))
    return out


def sfx_events(
    visuals: Any,
    window_of: WordWindow,
    duration: float,
    *,
    edl: EDL | None = None,
    preset: Preset | None = None,
    cfg: Settings | None = None,
) -> list[tuple[float, str]]:
    """Звуковые акценты графики: (секунда ролика, имя эффекта из `assets/sfx`).

    По акценту на вход каждого элемента и на начало и конец каждой сцены —
    смена того, что видит зритель, звуком только подтверждается (`plan_sfx`).

    Звук сводится РАНЬШЕ, чем собирается план, поэтому судьба элемента здесь
    известна только по времени: слово вырезано, сцены пересеклись, элемент попал
    на сцену. С листом (`edl`) учитывается и то, что знает сборка плана, — титр,
    вставки, списки, поделённый кадр. Одного отсюда не узнать никак: элемент,
    которому не нашлось места в кадре, выпадет уже при раскладке, и его акцент
    прозвучит впустую.
    """
    sheet = _sheet(visuals)
    if sheet is None or sheet.empty:
        return []
    blocked: list[tuple[float, float, str]] = []
    if edl is not None:
        blocked = _blockers(
            edl, window_of, duration, preset or load_preset(edl.preset), cfg or default_settings
        )
    scenes = _live_scenes(sheet, window_of, duration, blocked)
    overlays = _live_overlays(
        sheet, window_of, duration, scenes, edl.split_until if edl is not None else None
    )
    events: list[tuple[float, str]] = []
    for _, _, (start, end) in scenes:
        events.append((round(start, 3), SFX_SCENE_IN))
        events.append((round(end, 3), SFX_SCENE_OUT))
    for _, overlay, (start, _) in overlays:
        events.append((round(start, 3), SFX_OVERLAY[overlay.kind]))
    return sorted(events)


# --------------------------------------------------------------------------- движение сцены


def _field(obj: Any, name: str) -> Any:
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


def stage_at(scene: Any, frame: int, fps: int, width: int, height: int) -> dict[str, float]:
    """Окно говорящего и видео в нём на кадре `frame`.

    Зеркало формул из шапки `overlay_models.py`; вторая копия — `stageAt` в
    `render/src/visuals/motion.mjs`. Нужна Python, потому что трек лица во время
    сцены приходится пересчитывать: лицо стоит уже не там, где его снял кроп.

    `scene` — `PlanScene` или её словарь из `plan.meta["visuals"]`. Вне сцены
    `p = 0`: окно — весь кадр, видео стоит как стояло.
    """
    first = _round(float(_field(scene, "start")) * fps)
    last = _round(float(_field(scene, "end")) * fps)
    if frame <= first or frame >= last:
        p = 0.0
    else:
        p = min(
            smoothstep((frame - first) / max(1, int(_field(scene, "in_frames")))),
            smoothstep((last - frame) / max(1, int(_field(scene, "out_frames")))),
        )
    window = _field(scene, "window")
    if p == 1.0:
        # Покой — числа плана как есть, а не `W + 1·(w − W)`: у дробных значений
        # такая сумма расходится с исходным числом в последнем знаке. Рендерер
        # делает так же, и окно в покое у обоих совпадает бит в бит.
        return {
            "p": 1.0,
            "x": float(_field(window, "x")),
            "y": float(_field(window, "y")),
            "width": float(_field(window, "width")),
            "height": float(_field(window, "height")),
            "radius": float(_field(scene, "radius")),
            "tilt": float(_field(scene, "tilt")),
            "scale": float(_field(scene, "scale")),
            "tx": float(_field(scene, "tx")),
            "ty": float(_field(scene, "ty")),
        }
    return {
        "p": p,
        "x": p * float(_field(window, "x")),
        "y": p * float(_field(window, "y")),
        "width": width + p * (float(_field(window, "width")) - width),
        "height": height + p * (float(_field(window, "height")) - height),
        "radius": p * float(_field(scene, "radius")),
        "tilt": p * float(_field(scene, "tilt")),
        "scale": 1.0 + p * (float(_field(scene, "scale")) - 1.0),
        "tx": p * float(_field(scene, "tx")),
        "ty": p * float(_field(scene, "ty")),
    }


# --------------------------------------------------------------------------- стикеры


@lru_cache(maxsize=4)
def _read_index(path: str, stamp: int) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    stickers = data.get("stickers") if isinstance(data, dict) else None
    return stickers if isinstance(stickers, dict) else {}


def sticker_index() -> dict[str, dict[str, Any]]:
    """Набор стикеров: ключ → {emoji, file, aspect}. Нет файла набора — пусто.

    Кеш держится на времени правки файла: набор дополняют на живой машине.
    """
    path = STICKERS_DIR / "index.json"
    try:
        return _read_index(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError) as err:
        log.warning("набор стикеров не прочитан (%s) — стикеры рисуются глифами", err)
        return {}


def _image_aspect(path: Path) -> float | None:
    """Пропорция картинки (ширина/высота) по её заголовку; `None` — не прочиталась."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception as err:  # noqa: BLE001 — картинка не важнее ролика
        log.warning("картинка %s не прочитана (%s)", path, err)
        return None
    return width / height if width > 0 and height > 0 else None


@dataclass(frozen=True, slots=True)
class _Sticker:
    """Что рисовать стикером: файл с пропорцией или глиф цветного шрифта."""

    emoji: str | None
    asset: str | None = None
    aspect: float = 1.0


def _sticker(key: str | None, emoji: str | None, asset: str | None = None) -> _Sticker | None:
    """Стикер по заявке листа или `None`, если рисовать нечего.

    Порядок: готовый файл элемента (его кладёт Python — набор или генерация),
    потом ключ набора, потом голый эмодзи. Запись набора без файла — это глиф;
    так же заканчивается и запись, чей файл с диска пропал: стикер попроще
    лучше дыры в кадре.
    """
    glyph = single_emoji(emoji)
    if asset:
        path = Path(asset)
        return _Sticker(glyph, asset, (_image_aspect(path) if path.is_file() else None) or 1.0)
    entry = sticker_index().get(key) if key else None
    if isinstance(entry, dict):
        glyph = single_emoji(entry.get("emoji")) or glyph
        name = entry.get("file")
        if name:
            path = (STICKERS_DIR / str(name)).resolve()
            if path.is_file():
                aspect = entry.get("aspect") or _image_aspect(path) or 1.0
                return _Sticker(glyph, str(path), float(aspect))
            log.warning("файла стикера %r нет в %s — рисуется глиф", name, STICKERS_DIR)
    elif key:
        log.info("стикера %r в наборе нет", key)
    return _Sticker(glyph) if glyph else None


# --------------------------------------------------------------------------- плашка


@dataclass(slots=True)
class _Chip:
    """Измеренная плашка: коробка и поля `PlanOverlay`, которыми её рисуют."""

    width: float
    height: float
    look: dict[str, Any]


def chip_text(text: str, known: Any, *, upper: bool = False) -> str:
    """Текст плашки в том виде, в каком его наберёт шрифт темы.

    `known` — символы, которые в шрифте есть (`FontMetrics.advances`). Знак,
    которого нет, заменяется словом (`GLYPH_FALLBACKS`). Пунктуация не
    срезается, в отличие от слова субтитра: «−30 %» и «(до 5)» — это и есть
    содержание плашки.
    """
    shown = text
    for glyph, spare in GLYPH_FALLBACKS.items():
        if glyph in shown and glyph not in known:
            shown = shown.replace(glyph, spare)
    shown = " ".join(shown.split())
    return shown.upper() if upper else shown


def measure_chip(
    text: str,
    style: str,
    preset: Preset,
    frame_height: int,
    *,
    emoji: str | None = None,
    fill: str = "",
    color: str = "",
    max_width: float = math.inf,
    max_height: float = math.inf,
) -> _Chip | None:
    """Плашка с этим текстом или `None`, если её нечем измерить или она не влезла.

    Коробка считается той же машинерией, что плашка пункта списка
    (`render_plan.lay_list`): пресет плашки (`Pill.preset` — срезанная разрядка
    и поправка к пробелу), ширина строки по метрикам шрифта, высота строчного
    бокса. Поэтому она совпадает с Chromium в тех же двух пикселях, и рендерер
    может просто поставить текст по центру прямоугольника.

    Не влезла — ужимается шагами до пола своего вида; не влезла и так —
    выпадает. `preset` уже приведён к высоте кадра.
    """
    spec = CHIP_STYLES[style]
    pill = preset.caption.pill
    try:
        ruler = pill.preset(preset)
        metrics = metrics_for(ruler)
    except (FileNotFoundError, ValueError) as err:
        log.warning("плашка не измерена (%s) — выпадает", err)
        return None
    words = chip_text(text, metrics.advances, upper=spec.upper or preset.font.uppercase).split()
    if not words:
        return None
    icon = single_emoji(emoji) if style != "heading" else None
    ruler.caption.line_height = CHIP_LINE_HEIGHT
    k = frame_height / 1920

    scale = 1.0
    while scale >= spec.floor - 1e-9:
        size = max(8, int(spec.size * k * scale))
        ruler.font.size = size
        text_w = measure_line(
            [(word, float(size)) for word in words],
            metrics=metrics,
            letter_spacing=ruler.font.letter_spacing,
            base_size=float(size),
            word_spacing=ruler.font.word_spacing,
        )
        text_h = line_box_height([float(size)], ruler, metrics=metrics)
        pad_x, pad_y = round(size * spec.pad_x), round(size * spec.pad_y)
        # Окошко значка — добавка к левому полю, как у плашки пункта: ширина
        # коробки от настоящей ширины глифа не зависит.
        box = round(size * pill.emoji_box, 3) if icon else 0.0
        gap = 0.0
        if icon:
            gap = round(
                pill.emoji_gap * size / pill.font_size
                if pill.font_size and pill.emoji_gap
                else size * CHIP_EMOJI_GAP_EM,
                3,
            )
        width = text_w + box + gap + 2 * pad_x
        height = text_h + 2 * pad_y
        if width <= max_width and height <= max_height:
            break
        scale = round(scale - 0.05, 4)
    else:
        return None

    boxed = style != "heading"
    return _Chip(
        width=width,
        height=height,
        look={
            "style": style,
            "words": words,
            "font": "caption",
            "font_size": float(size),
            "letter_spacing": ruler.font.letter_spacing,
            "word_spacing": ruler.font.word_spacing,
            "line_height": CHIP_LINE_HEIGHT,
            "padding_x": float(pad_x),
            "padding_y": float(pad_y),
            "radius": (
                round(height / 2, 2) if spec.radius is None else round(size * spec.radius, 2)
            ),
            "fill": fill if boxed else "",
            "fill_opacity": 1.0 if boxed else 0.0,
            "color": color,
            "emoji": icon,
            "emoji_box": box,
            "emoji_size": round(size * pill.emoji_scale, 3) if icon else 0.0,
            "emoji_gap": gap,
        },
    )


# --------------------------------------------------------------------------- лицо


@dataclass(frozen=True, slots=True)
class _Face:
    """Лицо за промежуток времени, пиксели кадра.

    `cx`, `cy`, `h` — медианы (по ним выбирается место рядом с головой), `box` —
    объединение боксов «от глаз до подбородка» по всем кадрам промежутка: его
    графика закрыть не имеет права ни на одном кадре.
    """

    cx: float
    cy: float
    h: float
    box: Rect
    seen: bool


def _guard(cx: float, cy: float, h: float) -> Rect:
    """Бокс лица теми же константами, что у `render_plan.covers_face`."""
    half = FACE_WIDTH_RATIO * h / 2
    return Rect(cx - half, cy - FACE_GUARD_UP * h, 2 * half, (FACE_GUARD_UP + FACE_GUARD_DOWN) * h)


def face_at(
    track: FaceTrack | None, start: float, end: float, frame: tuple[int, int]
) -> _Face:
    """Лицо за время `start..end`; без трека и без уверенных точек — условное."""
    width, height = frame
    points = []
    if track is not None and track.points:
        for f in range(round(start * track.fps), round(end * track.fps) + 1):
            point = track.at(f)
            if point is not None and point.conf >= FACE_MIN_CONF:
                points.append(point)
    if not points:
        cx, cy, h = NOMINAL_FACE[0] * width, NOMINAL_FACE[1] * height, NOMINAL_FACE[2] * height
        return _Face(cx, cy, h, _guard(cx, cy, h), False)
    return _Face(
        cx=median(p.cx for p in points) * width,
        cy=median(p.cy for p in points) * height,
        h=median(p.h for p in points) * height,
        box=_union([_guard(p.cx * width, p.cy * height, p.h * height) for p in points]),
        seen=True,
    )


# --------------------------------------------------------------------------- содержимое сцены


@dataclass(slots=True)
class _Item:
    """Пункт сцены по дороге от листа к плану: что рисуем и какого он размера."""

    kind: str  # chip | sticker | photo | map
    source: SceneItem
    seed: int
    rotate: float = 0.0
    sticker: _Sticker | None = None
    aspect: float = 1.0
    # Заполняет подгонка под место (`_Dresser._fit`).
    size: tuple[float, float] = (0.0, 0.0)
    body: tuple[float, float] = (0.0, 0.0)  # место самой карточки, уже с наклоном
    lift: float = 0.0
    chip: _Chip | None = None
    label: _Chip | None = None
    glyph: float = 0.0


@dataclass(slots=True)
class _Group:
    """Пункты, идущие одним рядом или одной колонкой в своей области кадра."""

    items: list[_Item]
    region: Rect
    axis: str  # row | column
    align: str = "center"  # center | start — вдоль оси


@dataclass(slots=True)
class _Layout:
    """Раскладка сцены: окно говорящего, фон и области под содержимое."""

    window: Rect
    radius: float
    tilt: float
    bg: PlanSceneBg
    chrome: str = "none"
    bezel: float = 0.0
    bezel_color: str = "#000000"
    heading_at: tuple[float, float] | None = None  # левый верхний угол заголовка
    groups: list[_Group] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Palette:
    accent: str
    heading: str
    luma: Callable[[Rect], float] | None  # яркость фона под блоком; `None` — видео


# --------------------------------------------------------------------------- сборка


class _Dresser:
    """Раскладка графики по одному собранному плану.

    Всё, что знает о кадре: план (блоки и вставки с их временем и местом), тему,
    трек лица. Ничего не меняет в плане сам — только отдаёт готовые элементы.
    """

    def __init__(
        self,
        plan: RenderPlan,
        preset: Preset,
        track: FaceTrack | None,
        window_of: WordWindow,
    ) -> None:
        self.plan = plan
        self.ps = preset
        self.track = track
        self.window_of = window_of
        self.fps = plan.fps
        self.width = plan.width
        self.height = plan.height
        self.frame = (plan.width, plan.height)
        self.sx = safe_margin_x(preset, plan.width)
        self.sy = plan.height * preset.geometry.safe_area_ratio
        # Зазор между графикой и всем остальным. Перелёт входа (до 4 % масштаба)
        # двигает край элемента на пару процентов его размера — зазор его
        # покрывает, как `broll_margin_ratio` покрывает перелёт карточки.
        self.gap = round(0.0125 * plan.height)
        self.tight = round(0.0075 * plan.height)
        # Ниже этой линии — интерфейс ленты (`caption.margin`): графике там
        # делать нечего, хотя безопасная зона формально идёт ниже.
        self.floor = plan.height - max(self.sy, safe_margin_y(preset, plan.height))
        self.safe = Rect(
            self.sx, self.sy, plan.width - 2 * self.sx, plan.height - 2 * self.sy
        )

    # ---------------------------------------------------------------- общее

    def frame_of(self, seconds: float) -> int:
        return _round(seconds * self.fps)

    def frames(self, seconds: float) -> int:
        return max(MIN_ANIM_FRAMES, _round(seconds * self.fps))

    def motion(self, life: int, enter: float, leave: float) -> tuple[int, int] | None:
        """Кадры входа и выхода или `None`, если элемент слишком короток.

        Не короче `MIN_ANIM_FRAMES` и не длиннее трети жизни; когда оба условия
        разом не выполнить, элемент не ставится вовсе.
        """
        cap = life // 3
        if cap < MIN_ANIM_FRAMES:
            return None
        return min(self.frames(enter), cap), min(self.frames(leave), cap)

    def inside(self, box: Rect) -> bool:
        return (
            box.x >= self.safe.x - 1e-6
            and box.y >= self.safe.y - 1e-6
            and box.right <= self.safe.right + 1e-6
            and box.bottom <= self.safe.bottom + 1e-6
        )

    def block_zone(self, block: PlanBlock) -> Rect:
        """Место блока субтитра вместе с его дорогой за головой."""
        rect = block.rect
        if rect is None:
            # Блок не измерен (нет метрик шрифта) — уступаем резерв его полосы,
            # как это делает сама сборка плана.
            return caption_zone(self.ps, self.frame, block.position)
        if not block.anchor_path:
            return rect
        xs = [0.0, *(float(p[0]) for p in block.anchor_path)]
        ys = [0.0, *(float(p[1]) for p in block.anchor_path)]
        return Rect(
            rect.x + min(xs),
            rect.y + min(ys),
            rect.width + max(xs) - min(xs),
            rect.height + max(ys) - min(ys),
        )

    def text_zones(self, start: float, end: float) -> list[Rect]:
        return [
            self.block_zone(block)
            for block in self.plan.blocks
            if block.start < end and block.end > start
        ]

    def picture_zones(self, start: float, end: float) -> list[Rect]:
        return [
            item.rect
            for item in self.plan.broll
            if item.rect is not None and item.start < end and item.end > start
        ]

    # ---------------------------------------------------------------- свободные элементы

    def slot_rect(
        self, slot: str, width: float, height: float, face: _Face, band: tuple[float, float]
    ) -> Rect | None:
        """Место элемента в слоте или `None`, если слот в этом кадре не помещается.

        Слот задаёт горизонталь и желаемую высоту; по вертикали место зажимается
        в свободную полосу (`geometry.free_band`) — ту, где нет ни текста, ни
        вставки. Лицо в полосу не входит намеренно: оно не во всю ширину кадра,
        и рядом с ним стоять можно. С ним место сверяется отдельно.
        """
        left, right = self.sx, self.width - self.sx - width
        if right < left:
            return None
        head_top = face.cy - HEAD_UP * face.h
        chin = face.cy + FACE_GUARD_DOWN * face.h
        middle = min(max(face.cx - width / 2, left), right)
        if slot == "above_head":
            x, want, low, high = middle, head_top - self.gap - height, self.sy, head_top - self.gap
        elif slot == "chest":
            x, want = middle, chin + 0.55 * face.h
            low, high = chin + 0.3 * face.h, self.floor
        elif slot in ("beside_left", "beside_right"):
            reach = HEAD_SIDE * face.h + self.gap
            x = face.cx - reach - width if slot == "beside_left" else face.cx + reach
            x = min(max(x, left), right)
            want = face.cy - 0.15 * face.h - height / 2
            low, high = self.sy, chin + 0.6 * face.h
        elif slot in ("top_left", "top_right"):
            x = left if slot == "top_left" else right
            want, low, high = self.sy + self.tight, self.sy, 0.45 * self.height
        else:
            return None
        top, bottom = max(low, band[0]), min(high, band[1])
        if bottom - top < height:
            return None
        return Rect(x, min(max(want, top), bottom - height), width, height)

    def slot_order(self, overlay: Overlay, seed: int) -> list[str]:
        """Слоты элемента в порядке предпочтения.

        Заявленный листом слот идёт первым, но не единственным: занят — элемент
        ищет место дальше, а не выпадает. Сторона у парных слотов чередуется по
        зерну, чтобы два стикера подряд не вставали в один угол.
        """
        near, far = ("right", "left") if seed % 2 == 0 else ("left", "right")
        beside = [f"beside_{near}", f"beside_{far}"]
        top = [f"top_{near}", f"top_{far}"]
        if overlay.kind == "sticker":
            order = [*beside, "above_head", *top, "chest"]
        elif overlay.style == "label":
            order = [*beside, "above_head", "chest", *top]
        elif overlay.style == "tag":
            order = ["chest", "above_head", *top, *beside]
        else:
            order = ["above_head", "chest", *top, *beside]
        if overlay.slot != "auto":
            order = [overlay.slot, *(s for s in order if s != overlay.slot)]
        return order

    def place(
        self,
        overlay: Overlay,
        seed: int,
        sizes: Sequence[tuple[float, float]],
        rotate: float,
        lift: float,
        window: tuple[float, float],
        taken: Sequence[Rect],
    ) -> tuple[Rect, int] | None:
        """Прямоугольник покоя свободного элемента и номер взятого размера.

        `sizes` — размеры элемента от желаемого к наименьшему. Слот важнее
        размера: сначала перебираются размеры в первом слоте и только потом
        берётся следующий слот. `None` — места нет нигде, элемент выпадает.
        """
        start, end = window
        face = face_at(self.track, start, end, self.frame)
        solid = [*self.text_zones(start, end), *self.picture_zones(start, end)]
        band = free_band(self.ps, self.frame, solid)
        zones = [*solid, face.box, *taken]
        for slot in self.slot_order(overlay, seed):
            for n, size in enumerate(sizes):
                box = hit_box(Rect(0.0, 0.0, *size), rotate, lift)
                spot = self.slot_rect(slot, box.width, box.height, face, band)
                if spot is None or not self.inside(spot):
                    continue
                if any(spot.overlaps(zone, gap=self.gap) for zone in zones):
                    continue
                return _centered(spot, *size), n
        return None

    def decor(
        self,
        overlay: Overlay,
        seed: int,
        window: tuple[float, float],
        taken: Sequence[Rect],
    ) -> list[PlanMark] | None:
        """Россыпь знаков вокруг головы или `None`, если им негде встать.

        Кадр делится на клетки, в каждой — одно возможное место знака со своим
        кеглем. Места, где знак задел бы голову, текст, вставку или другой
        элемент, отбрасываются сразу. Из оставшихся знаки берутся по секторам
        вокруг головы, в каждом — ближайшее к ней место: так россыпь окружает
        голову, а не сбивается в угол, и при этом сама находит, где свободно, —
        на крупном плане, где по бокам от лица кадра уже нет, знаки встают
        рядом над головой.

        Меньше трёх знаков — это не россыпь, а мусор в кадре: декор выпадает.
        """
        start, end = window
        face = face_at(self.track, start, end, self.frame)
        solid = [*self.text_zones(start, end), *self.picture_zones(start, end)]
        band = free_band(self.ps, self.frame, solid)
        # Голова целиком, с причёской и шеей: знак на макушке читается как
        # дефект маски, а не как приём.
        head = Rect(
            face.box.x - 0.15 * face.h,
            face.box.y - 0.7 * face.h,
            face.box.width + 0.3 * face.h,
            face.box.height + 0.85 * face.h,
        )
        marks = self.scatter(overlay, seed, [*solid, head, *taken], head, band)
        if marks is None:
            # Крупный план: голова занимает кадр почти целиком, и вокруг неё
            # места нет. Тогда знакам разрешены волосы — но не лоб и не лицо:
            # запретная зона сжимается до лица с запасом на лоб.
            brow = Rect(
                face.box.x - 0.05 * face.h,
                face.box.y - 0.35 * face.h,
                face.box.width + 0.1 * face.h,
                face.box.height + 0.35 * face.h,
            )
            marks = self.scatter(overlay, seed, [*solid, brow, *taken], head, band)
        return marks

    def scatter(
        self,
        overlay: Overlay,
        seed: int,
        zones: Sequence[Rect],
        head: Rect,
        band: tuple[float, float],
    ) -> list[PlanMark] | None:
        """Знаки декора мимо зон `zones`, вокруг `head`; `None` — меньше трёх."""
        top, bottom = max(self.sy, band[0]), min(self.floor, band[1])
        left, right = self.sx, self.width - self.sx
        low, high = {"sparkle": (0.03, 0.06), "arrow": (0.045, 0.07)}.get(
            overlay.glyph or "", (0.05, 0.085)
        )
        rng = random.Random(seed)
        count = 5 + seed % 3
        cell = 0.07 * self.height
        cols, rows = int((right - left) // cell), int((bottom - top) // cell)
        if cols < 1 or rows < 1:
            return None
        step_x, step_y = (right - left) / cols, (bottom - top) / rows
        hub = (head.center_x, head.center_y)
        spots: list[tuple[int, float, PlanMark, Rect]] = []
        for row in range(rows):
            for col in range(cols):
                # Все числа знака тянутся из генератора ДО проверок: иначе
                # отброшенное место сдвигало бы случайность у всех следующих.
                size = round(self.height * rng.uniform(low, high), 1)
                x = left + (col + 0.5 + rng.uniform(-0.3, 0.3)) * step_x
                y = top + (row + 0.5 + rng.uniform(-0.3, 0.3)) * step_y
                tilt = round(rng.uniform(-18.0, 18.0), 1)
                opacity = round(rng.uniform(0.55, 0.95), 2)
                phase = round(rng.random(), 2)
                # Знак в покое не стоит: он покачивается и медленно всплывает.
                # Место под него берётся с запасом на этот ход — трансформ
                # рендерера прямоугольника покоя не видит. Запас входит и в
                # границы, к которым место прижимается у края кадра.
                side = down = (0.5 + MARK_SWAY) * size
                up = (0.5 + MARK_RISE) * size
                if right - left < 2 * side or bottom - top < up + down:
                    continue
                mark = PlanMark(
                    round(min(max(x, left + side), right - side), 1),
                    round(min(max(y, top + up), bottom - down), 1),
                    size,
                    tilt,
                    opacity,
                    phase,
                )
                box = mark_box(mark)
                reach = Rect(mark.x - side, mark.y - up, 2 * side, up + down)
                # Прижатое к краю место округлено до десятых: допуск на это.
                if not self.inside(reach.inset(0.06)):
                    continue
                if any(reach.overlaps(zone, gap=self.tight) for zone in zones):
                    continue
                turn = (math.atan2(mark.y - hub[1], mark.x - hub[0]) + math.pi) / (2 * math.pi)
                spots.append(
                    (
                        int(turn * count) % count,
                        math.hypot(mark.x - hub[0], mark.y - hub[1]),
                        mark,
                        box,
                    )
                )
        spots.sort(key=lambda spot: (spot[1], spot[2].x, spot[2].y))
        marks: list[PlanMark] = []
        boxes: list[Rect] = []
        grown = True
        while grown and len(marks) < count:
            grown = False
            for sector in range(count):
                if len(marks) >= count:
                    break
                for found in spots:
                    if found[0] != sector:
                        continue
                    # Знаки не слипаются: между ними остаётся хотя бы треть знака.
                    if any(found[3].overlaps(box, gap=0.35 * found[2].size) for box in boxes):
                        continue
                    marks.append(found[2])
                    boxes.append(found[3])
                    grown = True
                    break
        return marks if len(marks) >= 3 else None

    def overlays(
        self, live: Sequence[tuple[int, Overlay, tuple[float, float]]]
    ) -> list[PlanOverlay]:
        """Свободные элементы листа → элементы плана. Кому нет места — выпадает."""
        out: list[PlanOverlay] = []
        # Что уже стоит в кадре: время и занятое место. Лист может поставить два
        # элемента разом (`OVERLAY_MAX_VISIBLE`), и лечь друг на друга они не должны.
        standing: list[tuple[float, float, list[Rect]]] = []
        accent = self.ps.colors.accent
        for index, overlay, (begin, finish) in live:
            seed = overlay.seed or index + 1
            first, last = self.frame_of(begin), self.frame_of(finish)
            start, end = first / self.fps, last / self.fps
            fade = overlay.kind == "decor"
            moves = self.motion(
                last - first, FADE_IN_S if fade else POP_IN_S, FADE_OUT_S if fade else POP_OUT_S
            )
            if moves is None:
                log.info("элемент %d (%s) выпал: слишком короток", index, overlay.kind)
                continue
            taken = [r for a, b, rects in standing if a < end and b > start for r in rects]
            base: dict[str, Any] = dict(
                start=start, end=end, in_frames=moves[0], out_frames=moves[1], seed=seed
            )
            made: PlanOverlay | None = None
            held: list[Rect] = []

            if overlay.kind == "chip":
                fill = TAG_FILL if overlay.style == "tag" else accent
                chip = (
                    measure_chip(
                        overlay.text,
                        overlay.style,
                        self.ps,
                        self.height,
                        emoji=overlay.emoji,
                        fill=fill,
                        color=_ink_on(fill),
                        max_width=0.92 * self.safe.width,
                    )
                    if overlay.text and overlay.text.strip()
                    else None
                )
                if chip is not None:
                    rotate = _tilt(seed, 3.0, 5.0) if overlay.style == "tag" else 0.0
                    found = self.place(
                        overlay, seed, [(chip.width, chip.height)], rotate, 0.0, (start, end), taken
                    )
                    if found is not None:
                        made = PlanOverlay(
                            kind="chip",
                            rect=found[0],
                            rotate=rotate,
                            anim="pop",
                            **base,
                            **chip.look,
                        )
                        held = [hit_box(found[0], rotate)]

            elif overlay.kind == "sticker":
                picture = _sticker(overlay.sticker, overlay.emoji, overlay.asset)
                if picture is not None:
                    lift = float(round(STICKER_FLOAT * self.height))
                    rotate = _tilt(seed, 4.0, 8.0)
                    glyphs = [0.0 for _ in STICKER_SCALES]
                    if picture.asset:
                        aspect = picture.aspect
                        sides = [STICKER_SIDE * self.height * k for k in STICKER_SCALES]
                        sizes = [
                            (side, side / aspect) if aspect >= 1 else (side * aspect, side)
                            for side in sides
                        ]
                    else:
                        full = min(GLYPH_MAX_PX, GLYPH_RATIO * self.height)
                        glyphs = [float(math.floor(full * k)) for k in STICKER_SCALES]
                        sizes = [
                            (float(round(g * GLYPH_BOX[0])), float(round(g * GLYPH_BOX[1])))
                            for g in glyphs
                        ]
                    found = self.place(overlay, seed, sizes, rotate, lift, (start, end), taken)
                    if found is not None:
                        made = PlanOverlay(
                            kind="sticker",
                            rect=found[0],
                            rotate=rotate,
                            anim="pop",
                            emoji=picture.emoji,
                            asset=picture.asset,
                            glyph_size=glyphs[found[1]],
                            float_px=lift,
                            **base,
                        )
                        held = [hit_box(found[0], rotate, lift)]

            elif overlay.glyph:
                marks = self.decor(overlay, seed, (start, end), taken)
                if marks is not None:
                    boxes = [mark_box(mark) for mark in marks]
                    # Следующим элементам знак уступает место вместе со своим ходом.
                    held = [
                        Rect(
                            box.x - MARK_SWAY * box.width,
                            box.y - MARK_RISE * box.height,
                            box.width * (1 + 2 * MARK_SWAY),
                            box.height * (1 + MARK_RISE + MARK_SWAY),
                        )
                        for box in boxes
                    ]
                    made = PlanOverlay(
                        kind="decor",
                        rect=_union(boxes),
                        anim="fade",
                        glyph=overlay.glyph,
                        marks=marks,
                        color=accent if overlay.glyph in ("sparkle", "arrow") else INK_LIGHT,
                        **base,
                    )

            if made is None:
                log.info("элемент %d (%s) выпал: рисовать нечем или негде", index, overlay.kind)
                continue
            out.append(made)
            standing.append((start, end, held))
        return out

    # ---------------------------------------------------------------- сцены

    def scene_frames(self, start: float, end: float) -> tuple[int, int, int, int] | None:
        """Кадры сцены: начало, конец, вход и выход; `None` — сцена слишком коротка."""
        first, last = self.frame_of(start), self.frame_of(end)
        moves = self.motion(last - first, SCENE_IN_S, SCENE_OUT_S)
        return None if moves is None else (first, last, *moves)

    def palette(self, layout: str) -> _Palette:
        """Цвета содержимого сцены и яркость её фона под субтитром."""
        accent, text = self.ps.colors.accent, self.ps.colors.active
        if layout == "paper":
            return _Palette(
                accent=_accent_on(PAPER_COLOR, accent, PAPER_ACCENT),
                heading=PAPER_INK,
                luma=lambda rect: luminance(PAPER_COLOR),
            )
        if layout == "card_top":
            return _Palette(
                accent=_accent_on(BOARD_BOTTOM, accent, INK_LIGHT),
                heading=_accent_on(BOARD_BOTTOM, text, INK_LIGHT),
                # Доска — градиент сверху вниз: яркость берётся там, где стоит блок.
                luma=lambda rect: luminance(
                    _mix(BOARD_TOP, BOARD_BOTTOM, min(1.0, max(0.0, rect.center_y / self.height)))
                ),
            )
        # Телефон: субтитр лежит поверх самого видео, замер яркости остаётся в силе.
        return _Palette(
            accent=_accent_on(STILL_BOTTOM, accent, INK_LIGHT),
            heading=_accent_on(STILL_BOTTOM, text, INK_LIGHT),
            luma=None,
        )

    def scene_items(self, index: int, scene: Scene) -> list[_Item]:
        """Пункты сцены, которые есть чем нарисовать. Остальные выпадают здесь."""
        out: list[_Item] = []
        for n, source in enumerate(scene.items):
            seed = index * 10 + n + 1
            if source.kind == "chip":
                if source.text and source.text.strip():
                    out.append(_Item("chip", source, seed))
            elif source.kind == "sticker":
                picture = _sticker(source.sticker, source.emoji, source.asset)
                if picture is not None:
                    if picture.asset and source.asset_aspect:
                        picture = _Sticker(picture.emoji, picture.asset, source.asset_aspect)
                    out.append(
                        _Item(
                            "sticker",
                            source,
                            seed,
                            rotate=_tilt(seed, 4.0, 8.0),
                            sticker=picture,
                            aspect=picture.aspect if picture.asset else GLYPH_BOX[0] / GLYPH_BOX[1],
                        )
                    )
            elif source.kind == "photo":
                if not source.asset:
                    log.info("фото в сцене %d выпало: картинки нет", index)
                    continue
                path = Path(source.asset)
                aspect = source.asset_aspect or (
                    _image_aspect(path) if path.is_file() else None
                )
                out.append(
                    _Item(
                        "photo",
                        source,
                        seed,
                        rotate=_tilt(seed, 2.0, 4.0),
                        aspect=min(2.0, max(0.5, aspect or PHOTO_ASPECT)),
                    )
                )
            else:
                out.append(_Item("map", source, seed, rotate=_tilt(seed, 1.5, 2.5)))
        return out

    def _fit(
        self, item: _Item, max_w: float, max_h: float, axis: str, palette: _Palette
    ) -> tuple[float, float] | None:
        """Подгоняет пункт под место и отдаёт, сколько он займёт; `None` — не влез.

        Занятое место считается уже с наклоном и покачиванием (`hit_box`):
        соседи разводятся по нему, а не по прямоугольнику покоя.
        """
        height = self.height
        item.label = None
        if item.kind == "chip":
            fill = palette.accent
            chip = measure_chip(
                item.source.text or "",
                "pill",
                self.ps,
                height,
                emoji=item.source.emoji,
                fill=fill,
                color=_ink_on(fill),
                max_width=max_w,
                max_height=max_h,
            )
            if chip is None:
                return None
            item.chip, item.size = chip, (chip.width, chip.height)
            item.body = item.size
            return item.size

        angle = math.radians(abs(item.rotate))
        cos, sin = math.cos(angle), math.sin(angle)

        def tallest(aspect: float, room_w: float, room_h: float) -> float:
            """Наибольшая высота карточки с этой пропорцией и наклоном в этом месте."""
            return min(room_w / (aspect * cos + sin), room_h / (cos + aspect * sin))

        if item.kind == "sticker":
            item.lift = float(round(STICKER_FLOAT * height))
            tall = tallest(item.aspect, max_w, max_h - 2 * item.lift)
            assert item.sticker is not None
            if item.sticker.asset:
                side = SCENE_STICKER_SIDE * height
                tall = min(tall, side / max(item.aspect, 1.0))
                if tall < 0.035 * height:
                    return None
                item.glyph = 0.0
                item.size = (tall * item.aspect, tall)
            else:
                glyph = math.floor(
                    min(GLYPH_MAX_PX, GLYPH_RATIO * height, tall / GLYPH_BOX[1])
                )
                if glyph < 0.028 * height:
                    return None
                item.glyph = float(glyph)
                item.size = (float(round(glyph * GLYPH_BOX[0])), float(round(glyph * GLYPH_BOX[1])))
            box = hit_box(Rect(0.0, 0.0, *item.size), item.rotate, item.lift)
            item.body = (box.width, box.height)
            return item.body

        # Карточки: фото и карта. Подпись фото стоит ПОД карточкой, метка карты —
        # на самой карте, рядом с точкой, и места не добавляет.
        text = (item.source.text or "").strip()
        label = (
            measure_chip(
                text, "label", self.ps, height, fill=palette.accent, color=_ink_on(palette.accent)
            )
            if text
            else None
        )
        if item.kind == "map":
            # В ряду карта лежит, в узкой колонке — квадратная: лежачая карта
            # шириной в колонку телефона была бы полоской.
            item.aspect = 1.45 if axis == "row" else (1.3 if max_w >= 0.25 * self.width else 1.0)
            under = 0.0
        else:
            under = label.height + self.tight if label is not None else 0.0
        tall = min(tallest(item.aspect, max_w, max_h - under), 0.21 * height)
        wide = min(tall * item.aspect, 0.45 * self.width)
        tall = wide / item.aspect
        if tall < 0.055 * height or wide < 0.11 * self.width:
            if under and item.kind == "photo":
                # Подпись съела место — карточка без подписи лучше, чем никакой.
                under, label = 0.0, None
                tall = min(tallest(item.aspect, max_w, max_h), 0.21 * height)
                wide = min(tall * item.aspect, 0.45 * self.width)
                tall = wide / item.aspect
            if tall < 0.055 * height or wide < 0.11 * self.width:
                return None
        item.size = (wide, tall)
        box = hit_box(Rect(0.0, 0.0, wide, tall), item.rotate)
        item.body = (box.width, box.height)
        if label is not None:
            # Подпись не шире своей карточки. Не ужалась до её ширины — карточка
            # остаётся как посчитана, просто без подписи.
            room = box.width if item.kind == "photo" else wide - 4 * self.tight
            if label.width > room:
                label = measure_chip(
                    text,
                    "label",
                    self.ps,
                    height,
                    fill=palette.accent,
                    color=_ink_on(palette.accent),
                    max_width=room,
                )
        item.label = label
        return box.width, box.height + (under if label is not None else 0.0)

    def pack(self, group: _Group, palette: _Palette) -> list[tuple[_Item, Rect]]:
        """Расставляет пункты группы в её области: (пункт, занятое им место).

        Пункты, которые вместе не помещаются, ужимаются пропорционально; тот,
        кто ужаться не может, уступает — выпадает последний по листу, и
        раскладка считается заново.
        """
        items = list(group.items)
        region, row = group.region, group.axis == "row"
        if region.width <= 0 or region.height <= 0:
            return []
        main = 0 if row else 1
        length = region.width if row else region.height
        while items:
            # Сначала каждый меряется так, будто область его одного: кто не
            # влез даже так, не влезет никак.
            alone = [self._fit(i, region.width, region.height, group.axis, palette) for i in items]
            if any(size is None for size in alone):
                items = [item for item, size in zip(items, alone) if size is not None]
                continue
            sizes = [size for size in alone if size is not None]
            room = length - self.tight * (len(items) - 1)
            total = sum(size[main] for size in sizes)
            if total > room:
                share = room / total
                shrunk = [
                    self._fit(
                        item,
                        size[0] * share if row else region.width,
                        region.height if row else size[1] * share,
                        group.axis,
                        palette,
                    )
                    for item, size in zip(items, sizes)
                ]
                if any(size is None for size in shrunk):
                    items.pop()
                    continue
                sizes = [size for size in shrunk if size is not None]
            used = sum(size[main] for size in sizes) + self.tight * (len(items) - 1)
            at = (region.x if row else region.y) + (
                (length - used) / 2 if group.align == "center" else 0.0
            )
            placed: list[tuple[_Item, Rect]] = []
            for item, (wide, tall) in zip(items, sizes):
                if row:
                    spot = Rect(at, region.y + (region.height - tall) / 2, wide, tall)
                    at += wide + self.tight
                else:
                    spot = Rect(region.x + (region.width - wide) / 2, at, wide, tall)
                    at += tall + self.tight
                placed.append((item, spot))
            return placed
        return []

    def emit(
        self, item: _Item, spot: Rect, palette: _Palette, times: dict[str, Any]
    ) -> list[PlanOverlay]:
        """Пункт на своём месте → элементы плана (карточка и её подпись)."""
        if item.kind == "chip":
            assert item.chip is not None
            return [
                PlanOverlay(
                    kind="chip",
                    rect=_centered(spot, *item.size),
                    anim="pop",
                    seed=item.seed,
                    **times,
                    **item.chip.look,
                )
            ]
        if item.kind == "sticker":
            assert item.sticker is not None
            return [
                PlanOverlay(
                    kind="sticker",
                    rect=_centered(spot, *item.size),
                    rotate=item.rotate,
                    anim="pop",
                    seed=item.seed,
                    emoji=item.sticker.emoji,
                    asset=item.sticker.asset,
                    glyph_size=item.glyph,
                    float_px=item.lift,
                    **times,
                )
            ]
        body = Rect(spot.center_x - item.body[0] / 2, spot.y, *item.body)
        rect = _centered(body, *item.size)
        frame = dict(
            radius=float(round(0.011 * self.height)),
            border_width=float(max(2, round(0.005 * self.height))),
            border_color=CARD_BORDER,
        )
        out: list[PlanOverlay] = []
        label_at: tuple[float, float] | None = None
        if item.kind == "photo":
            out.append(
                PlanOverlay(
                    kind="photo",
                    rect=rect,
                    rotate=item.rotate,
                    anim="pop",
                    seed=item.seed,
                    asset=item.source.asset,
                    **frame,
                    **times,
                )
            )
            if item.label is not None:
                label_at = (spot.center_x - item.label.width / 2, body.bottom + self.tight)
        else:
            # Точка стоит в верхней половине карты, чуть в стороне от центра
            # (сторона — по зерну): так под ней остаётся место для подписи.
            rng = random.Random(item.seed)
            pin_x = rect.width * rng.uniform(0.38, 0.62)
            pin_y = rect.height * rng.uniform(0.34, 0.44)
            out.append(
                PlanOverlay(
                    kind="map",
                    rect=rect,
                    rotate=item.rotate,
                    anim="pop",
                    seed=item.seed,
                    color=palette.accent,
                    pin={"x": round(pin_x, 2), "y": round(pin_y, 2)},
                    **frame,
                    **times,
                )
            )
            if item.label is not None:
                edge = 2 * self.tight
                x = min(
                    max(rect.x + pin_x - item.label.width / 2, rect.x + edge),
                    rect.right - edge - item.label.width,
                )
                y = min(rect.y + pin_y + self.tight, rect.bottom - edge - item.label.height)
                # Метке на карте тесно — тогда карта остаётся с одной точкой.
                if y >= rect.y + pin_y and x >= rect.x + edge - 1e-6:
                    label_at = (x, y)
        if item.label is not None and label_at is not None:
            out.append(
                PlanOverlay(
                    kind="chip",
                    rect=Rect(label_at[0], label_at[1], item.label.width, item.label.height),
                    rotate=item.rotate if item.kind == "map" else 0.0,
                    anim="pop",
                    seed=item.seed,
                    **times,
                    **item.label.look,
                )
            )
        return out

    def _card_top(
        self, caption_top: float, heading: _Chip | None, items: list[_Item]
    ) -> _Layout:
        """Говорящий в карточке наверху, под ним тёмная доска с содержимым."""
        width, height = self.frame
        side = width * self.ps.geometry.broll_margin_ratio
        top = round(0.05 * height)
        bottom = caption_top - self.gap
        row = 0.115 * height if items else 0.0
        need = (heading.height + self.gap if heading else 0.0) + row
        # Окно отдаёт доске ровно столько, сколько нужно её содержимому, но в
        # своих границах: ниже 30 % высоты кадра лицо в нём уже мелкое.
        tall = min(max(bottom - self.gap - need - top, 0.3 * height), 0.42 * height)
        window = Rect(side, float(top), width - 2 * side, float(round(tall)))
        band_top = window.bottom + self.gap
        heading_at = None
        # Доске тесно (высокий субтитр) — заголовок уступает содержимому: пункты
        # привязаны к словам речи, а заголовок только подписывает доску.
        cramped = items and bottom - band_top - (heading.height if heading else 0.0) < 0.07 * height
        if heading is not None and not cramped:
            heading_at = (self.sx + (self.safe.width - heading.width) / 2, band_top)
            band_top += heading.height + self.gap
        region = Rect(self.sx, band_top, self.safe.width, bottom - band_top)
        return _Layout(
            window=window,
            radius=float(round(0.05 * width)),
            tilt=0.0,
            bg=PlanSceneBg(kind="gradient", color=BOARD_TOP, color2=BOARD_BOTTOM),
            heading_at=heading_at,
            groups=[_Group(items, region, "row")] if items else [],
        )

    def _paper(
        self, index: int, caption_top: float, heading: _Chip | None, items: list[_Item]
    ) -> _Layout:
        """Лист в клетку: говорящий в наклонённой карточке справа, содержимое слева.

        Плашки идут рядом над субтитром во всю ширину — в левой колонке им
        тесно; стикеры, фото и карты стоят колонкой слева от окна.
        """
        width, height = self.frame
        side = width * self.ps.geometry.broll_margin_ratio
        tilt = 3.0 if index % 2 == 0 else -3.0
        sin = math.sin(math.radians(abs(tilt)))
        chips = [item for item in items if item.kind == "chip"]
        cards = [item for item in items if item.kind != "chip"]
        wide = float(round(0.52 * width))
        # Наклон раздвигает карточку: на столько её углы выходят за свой
        # прямоугольник. По вертикали оценка сверху (от ширины), по горизонтали
        # считается после того, как известна высота.
        spread_y = wide * sin / 2
        row = 0.0
        if chips:
            probe = measure_chip(chips[0].source.text or "", "pill", self.ps, height)
            row = (probe.height if probe else 0.055 * height) + self.gap
        top = self.sy + (heading.height + self.gap if heading else 0.0)
        bottom = caption_top - self.gap - row
        tall = min(0.42 * height, max(0.2 * height, bottom - top - 2 * spread_y))
        spread_x = tall * sin / 2
        window = Rect(
            float(round(width - side - spread_x - wide)),
            float(round(top + spread_y + max(0.0, bottom - top - 2 * spread_y - tall) / 2)),
            wide,
            float(round(tall)),
        )
        shadow = hit_box(window, tilt)
        groups: list[_Group] = []
        if cards:
            column = Rect(self.sx, shadow.y, shadow.x - self.gap - self.sx, shadow.height)
            groups.append(_Group(cards, column, "column"))
        if chips:
            strip = Rect(self.sx, bottom + self.gap, self.safe.width, row - self.gap)
            groups.append(_Group(chips, strip, "row", align="start"))
        return _Layout(
            window=window,
            radius=float(round(0.04 * width)),
            tilt=tilt,
            bg=PlanSceneBg(
                kind="paper",
                color=PAPER_COLOR,
                color2=PAPER_COLOR,
                grid=float(round(0.028 * height)),
                line_color=PAPER_LINE,
            ),
            heading_at=(self.sx, self.sy) if heading is not None else None,
            groups=groups,
        )

    def _phone(
        self, caption_top: float, heading: _Chip | None, items: list[_Item]
    ) -> _Layout:
        """Видео в рамке телефона по центру, под ним размытый кадр ролика.

        Экран — ровно 9:16, как сам кадр: видео в нём видно целиком, и лицо из
        окна уйти не может. Плашки, фото и карты стоят рядом над телефоном:
        по бокам от него остаётся полоса в седьмую часть кадра, и карточка в
        ней выходит маркой. Стикерам этой полосы хватает — они встают по бокам
        от телефона, выше субтитра (он шире экрана телефона).
        """
        width, height = self.frame
        bezel = float(round(0.016 * width))
        tall = float(round(0.54 * height))
        wide = tall * width / height
        x = (width - wide) / 2
        y = min(self.floor, height - self.sy) - bezel - tall
        window = Rect(x, y, wide, tall)
        band_top = self.sy
        heading_at = None
        if heading is not None:
            heading_at = (self.sx + (self.safe.width - heading.width) / 2, band_top)
            band_top += heading.height + self.gap
        beside = [item for item in items if item.kind == "sticker"]
        above = [item for item in items if item.kind != "sticker"]
        groups: list[_Group] = []
        if above:
            strip = Rect(self.sx, band_top, self.safe.width, y - bezel - self.gap - band_top)
            groups.append(_Group(above, strip, "row"))
        if beside:
            tall_room = caption_top - self.gap - y
            gutter = x - bezel - self.tight - self.sx
            right = Rect(x + wide + bezel + self.tight, y, gutter, tall_room)
            left = Rect(self.sx, y, gutter, tall_room)
            # Через одного: первый стикер справа, второй слева — колонки
            # заполняются поровну, а не одна до упора.
            groups.append(_Group(beside[0::2], right, "column"))
            if beside[1::2]:
                groups.append(_Group(beside[1::2], left, "column"))
        return _Layout(
            window=window,
            radius=float(round(0.045 * width)),
            tilt=0.0,
            bg=PlanSceneBg(kind="still", color=STILL_TOP, color2=STILL_BOTTOM),
            chrome="phone",
            bezel=bezel,
            bezel_color=PHONE_BEZEL,
            heading_at=heading_at,
            groups=groups,
        )

    def video_in(self, window: Rect, face: _Face) -> tuple[float, float, float]:
        """Масштаб и сдвиг видео внутри окна: `(scale, tx, ty)`.

        Масштаб — наименьший, при котором видео закрывает окно целиком: чем
        меньше обрезано, тем больше от кадра осталось, а щелей под окном быть
        не может. Сдвиг ставит лицо в центр окна по горизонтали, а по вертикали
        — на 42 % его высоты, ниже, если иначе срежется макушка. Жёсткое
        условие одно: лицо от глаз до подбородка обязано быть в окне. На очень
        крупном плане оно выше окна — тогда в окне по центру стоит именно эта
        часть лица, а макушка уходит за край.

        Масштаб округляется ВВЕРХ, а границы сдвига — внутрь: видео, не
        дотянувшее до края окна на сотую пикселя, — это щель в кадре.
        """
        width, height = self.frame
        scale = math.ceil(max(window.width / width, window.height / height) * 1e4) / 1e4
        face_h = scale * face.h
        edge = 0.04 * window.height
        low = FACE_GUARD_UP * face_h + edge
        high = window.height - FACE_GUARD_DOWN * face_h - edge
        if low > high:
            anchor = window.height / 2 - (FACE_GUARD_DOWN - FACE_GUARD_UP) * face_h / 2
        else:
            wish = max(0.42 * window.height, HEAD_UP * face_h + 0.03 * window.height)
            anchor = min(max(wish, low), high)
        low_x = math.ceil((window.width - scale * width) * 100) / 100
        low_y = math.ceil((window.height - scale * height) * 100) / 100
        tx = min(0.0, max(low_x, round(window.width / 2 - scale * face.cx, 2)))
        ty = min(0.0, max(low_y, round(anchor - scale * face.cy, 2)))
        return scale, tx + 0.0, ty + 0.0

    def scene(self, index: int, scene: Scene, start: float, end: float) -> PlanScene | None:
        """Сцена листа → сцена плана. `None` — сцена короче своей анимации."""
        frames = self.scene_frames(start, end)
        if frames is None:
            return None
        first, last, enter, leave = frames
        fps = self.fps
        start, end = first / fps, last / fps
        # Содержимое живёт, только пока окно стоит на месте: между входом и
        # выходом сцены. Так пункт ни на одном кадре не оказывается под окном,
        # которое ещё едет через его место.
        rest_from, rest_to = first + enter, last - leave

        lower = [
            zone for zone in self.text_zones(start, end) if zone.center_y > self.height / 2
        ]
        caption_top = min((zone.y for zone in lower), default=self.floor)
        palette = self.palette(scene.layout)

        heading = (
            measure_chip(
                scene.heading,
                "heading",
                self.ps,
                self.height,
                color=palette.heading,
                max_width=self.safe.width,
            )
            if scene.heading and scene.heading.strip()
            else None
        )
        items = self.scene_items(index, scene)
        if scene.layout == "card_top":
            layout = self._card_top(caption_top, heading, items)
        elif scene.layout == "paper":
            layout = self._paper(index, caption_top, heading, items)
        else:
            layout = self._phone(caption_top, heading, items)

        # Окно — уже таким, каким оно уедет в план (сотые пикселя): видео
        # подгоняется под то окно, которое нарисует рендерер, а не под то, что
        # было до округления, — иначе между ними остаётся щель в ту же сотую.
        window = layout.window.rounded()
        scale, tx, ty = self.video_in(window, face_at(self.track, start, end, self.frame))
        made = PlanScene(
            layout=scene.layout,
            start=start,
            end=end,
            in_frames=enter,
            out_frames=leave,
            window=window,
            radius=layout.radius,
            tilt=layout.tilt,
            scale=scale,
            tx=tx,
            ty=ty,
            bg=layout.bg,
            chrome=layout.chrome,
            bezel=layout.bezel,
            bezel_color=layout.bezel_color,
        )

        need = max(3 * MIN_ANIM_FRAMES, _round(ITEM_MIN_LIFE_S * fps))
        if rest_to - rest_from < need:
            return made  # сцена есть, но содержимому в ней не простоять

        # Что содержимому нельзя задевать: окно (с рамкой и наклоном) и всё,
        # что уже встало. Субтитры сверяются по времени каждого пункта отдельно.
        solid = [hit_box(window.inset(-layout.bezel), layout.tilt)]

        def fits(parts: Sequence[PlanOverlay]) -> bool:
            """Можно ли поставить элементы: в безопасной зоне и ни на чём не лежат."""
            for part in parts:
                box = hit_box(part.rect, part.rotate, part.float_px)
                if not self.inside(box):
                    return False
                zones = [*solid, *self.text_zones(part.start, part.end)]
                if any(box.overlaps(zone) for zone in zones):
                    return False
            return True

        def timing(at: int, stop: int) -> dict[str, Any] | None:
            moves = self.motion(stop - at, POP_IN_S, POP_OUT_S)
            if moves is None:
                return None
            return dict(start=at / fps, end=stop / fps, in_frames=moves[0], out_frames=moves[1])

        if heading is not None and layout.heading_at is not None:
            times = timing(rest_from, rest_to)
            if times is not None:
                title = PlanOverlay(
                    kind="chip",
                    rect=Rect(*layout.heading_at, heading.width, heading.height),
                    anim="slide",
                    seed=index + 1,
                    **times,
                    **heading.look,
                )
                if fits([title]):
                    made.heading = title
                    solid.append(title.rect)
                else:
                    log.info("заголовок сцены %d выпал: ему нет места", index)

        placed: list[tuple[_Item, Rect, int]] = []
        for group in layout.groups:
            for item, spot in self.pack(group, palette):
                spoken = self.window_of(item.source.word_idx)
                if spoken is None:
                    log.info("пункт сцены %d выпал: его слово вырезано", index)
                    continue
                placed.append((item, spot, max(self.frame_of(spoken[0]), rest_from)))
        # Время — в порядке листа: раскладка по областям порядок перемешивает.
        placed.sort(key=lambda entry: entry[0].seed)
        # Вход на своём слове, но не позже, чем пункту останется простоять своё.
        # Очередь считается С КОНЦА: когда слова пунктов стоят у самого края
        # сцены, раньше приходится входить всем, и порядок с шагом между ними
        # обязан пережить этот сдвиг — иначе пункты влетают одним кадром.
        stagger = max(1, _round(ITEM_STAGGER_S * fps))
        latest = rest_to - need
        starts: list[int] = []
        ceiling = latest
        for _, _, wish in reversed(placed):
            starts.append(max(rest_from, min(wish, ceiling)))
            ceiling = starts[-1] - stagger
        starts.reverse()
        # И в другую сторону: пункты, чьи слова прозвучали до того, как окно
        # встало, упираются в один и тот же первый кадр покоя — они расходятся
        # тем же шагом, пока это не отнимает у последнего его срок.
        for n in range(1, len(starts)):
            starts[n] = min(latest, max(starts[n], starts[n - 1] + stagger))
        for (item, spot, _), at in zip(placed, starts):
            times = timing(at, rest_to)
            if times is None:
                continue
            parts = self.emit(item, spot, palette, times)
            if not fits(parts):
                log.info("пункт сцены %d (%s) выпал: ему нет места", index, item.kind)
                continue
            made.items.extend(parts)
            # Подпись карты лежит на своей карте намеренно — это одна вещь, и
            # соседям достаточно обойти карту.
            solid.append(hit_box(parts[0].rect, parts[0].rotate, parts[0].float_px))
            solid.extend(p.rect for p in parts[1:] if item.kind != "map")
        return made


# --------------------------------------------------------------------------- вход


def _staged_edl(
    edl: EDL, scenes: Sequence[tuple[int, Scene, tuple[float, float]]], window_of: WordWindow
) -> tuple[EDL, dict[int, int]]:
    """Копия листа, где блоки сцен стоят внизу плакатом, и слова этих блоков.

    Во время сцены кадр перекроен: наверху окно говорящего или заголовок, сбоку
    содержимое. Тихому блоку у левого края, плашке списка и блоку, едущему за
    головой, там встать негде, а голова к тому же уехала в окно. Блок
    переписывается целиком и дальше идёт через общий код обычным блоком с явной
    позицией — тот же приём, что у поделённого кадра (`render_plan`).

    Копия живёт один вызов `build_plan` и никуда не сохраняется: в сохранённом
    листе блоки остаются такими, какими их поставил режиссёр. Второе значение —
    номер слова → номер сцены, по нему блоки плана находятся после сборки.
    """
    staged = edl.model_copy(deep=True)
    words: dict[int, int] = {}
    for at, block in enumerate(staged.captions):
        hit: int | None = None
        for word in block.words:
            window = window_of(word.word_idx)
            if window is None:
                continue
            hit = next((i for i, _, span in scenes if _overlap(window, span)), None)
            if hit is not None:
                break
        if hit is None:
            continue
        staged.captions[at] = block.model_copy(
            update={
                "position": "bottom",
                "quiet": False,
                "anchor_track": None,
                "panel": False,
                "item": False,
            }
        )
        words.update({word.word_idx: hit for word in block.words})
    return staged, words


def dressed_plan(
    build_plan: Callable[..., RenderPlan],
    edl: EDL,
    video_path: str,
    *,
    track: FaceTrack | None = None,
    visuals: Any = None,
    **plan_args: Any,
) -> RenderPlan:
    """`build_plan` вместе с графикой: план, в чьём `meta["visuals"]` лежит раскладка.

    Шов в `camera.filmed_plan`: вместо `build_plan(edl, video_path, track=track,
    **plan_args)` зовётся `dressed_plan(build_plan, edl, video_path, track=track,
    **plan_args)`. Трек приходит уже после камеры — графика, как и субтитры,
    смотрит на лицо там, где его увидит зритель.

    `visuals` — лист графики; по умолчанию берётся из листа ролика
    (`EDL.visuals`). Без графики план возвращается ровно таким, каким его
    собрал бы `build_plan`: ни ключа в `meta`, ни тронутого блока.

    Порядок обязателен. Сначала решается судьба сцен — по времени, до сборки:
    от неё зависит, какие блоки переписывать. Потом собирается план. И только по
    готовому плану ставится всё остальное: место элемента — это то, что осталось
    от кадра после субтитров, вставок и лица.
    """
    sheet = _sheet(visuals if visuals is not None else getattr(edl, "visuals", None))
    if sheet is None or sheet.empty:
        return build_plan(edl, video_path, track=track, **plan_args)

    cfg: Settings = plan_args.get("cfg") or default_settings
    fps = plan_args.get("fps") or cfg.render_fps
    height = plan_args.get("height") or cfg.render_height
    preset = (plan_args.get("preset") or load_preset(edl.preset)).scaled(height)
    cut = plan_args.get("apply_cuts", True)
    timeline = edl.timeline() if cut else Timeline([], edl.source_duration)
    window_of = _word_windows(edl, timeline)
    duration = timeline.duration

    # Сцена, которой не хватает кадров на собственную анимацию, отсеивается
    # здесь же: после сборки плана её блоки были бы уже переписаны зря.
    scenes = [
        (index, scene, window)
        for index, scene, window in _live_scenes(
            sheet, window_of, duration, _blockers(edl, window_of, duration, preset, cfg)
        )
        if (_round(window[1] * fps) - _round(window[0] * fps)) // 3 >= MIN_ANIM_FRAMES
    ]
    overlays = _live_overlays(sheet, window_of, duration, scenes, edl.split_until)

    staged, scene_words = _staged_edl(edl, scenes, window_of) if scenes else (edl, {})
    plan = build_plan(staged, video_path, track=track, **plan_args)

    dresser = _Dresser(plan, Preset.model_validate(plan.preset), track, window_of)
    made = PlanVisuals()
    staged_scenes: dict[int, tuple[PlanScene, _Palette]] = {}
    for index, scene, (start, end) in scenes:
        built = dresser.scene(index, scene, start, end)
        if built is not None:
            made.scenes.append(built)
            staged_scenes[index] = (built, dresser.palette(scene.layout))
    made.overlays = dresser.overlays(overlays)

    # Яркость фона под блоками сцены — уже не яркость видео: под ними лежит
    # бумага или доска. Замер (`pipeline/luma.py`) про это не знает и знать не
    # может, он идёт по видео; поэтому число подменяется здесь, по готовой
    # раскладке. У телефона субтитр лежит поверх самого видео — замер остаётся.
    #
    # Блок на границе сцены лежит и на видео, и на фоне сцены, а число у него
    # одно. Оно описывает то, на чём блок лежит дольше: блоку, который задел
    # сцену хвостом, замер по видео нужнее.
    for block in plan.blocks:
        if block.title_card or block.pill is not None or block.rect is None:
            continue
        index = next(
            (scene_words[w.word_idx] for w in block.words if w.word_idx in scene_words), None
        )
        if index is None or index not in staged_scenes:
            continue
        staged_scene, palette = staged_scenes[index]
        inside = min(block.end, staged_scene.end) - max(block.start, staged_scene.start)
        if palette.luma is not None and inside > (block.end - block.start) / 2:
            block.bg_luma = round(palette.luma(block.rect), 4)
            block.ink = light_ink(block.bg_luma)

    if not made.empty and isinstance(plan.meta, dict):
        plan.meta["visuals"] = made.to_meta()
        log.info(
            "графика: элементов %d из %d, сцен %d из %d",
            len(made.overlays), len(sheet.overlays), len(made.scenes), len(sheet.scenes),
        )
    return plan


# --------------------------------------------------------------------------- файлы рендера


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b) or filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _stage(src: str | Path, assets_dir: Path) -> str | None:
    """Кладёт файл в папку задачи; имя относительно неё или `None`, если файла нет.

    Та же идея, что у `render._stage_asset`: Node монтирует папку задачи в бандл
    одной ссылкой, поэтому всё нарисованное обязано лежать в ней. Отличие одно —
    два РАЗНЫХ файла с одним именем (картинки из разных папок кеша) друг друга
    не затирают: второй получает имя с отпечатком своего пути.
    """
    path = Path(src)
    if not path.is_absolute():
        if (assets_dir / path).is_file():
            return path.as_posix()  # уже лежит в папке задачи — повторный вызов
        path = path.resolve()
    if not path.is_file():
        return None
    if path.parent.resolve() == assets_dir.resolve():
        return path.name
    dst = assets_dir / path.name
    if dst.exists() and not _same_file(path, dst):
        mark = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        dst = assets_dir / f"{path.stem}-{mark}{path.suffix}"
    if not dst.exists():
        try:
            os.link(path, dst)  # жёсткая ссылка: без копирования
        except OSError:
            shutil.copy2(path, dst)
    return dst.name


def _still(video: Path, at: float, out: Path, frame: tuple[int, int]) -> bool:
    """Размытый затемнённый кадр ролика одним вызовом ffmpeg. `False` — не вышло.

    Размытие делается на мелкой копии: кадр ужимается в восемь раз, размывается
    и растягивается обратно до половины кадра — это на порядок дешевле
    размытия полного кадра, а на глаз то же самое. Затемнение — умножением
    каналов, а не сдвигом яркости: сдвиг выбивает тени в ровный чёрный.
    """
    from src.core.proc import ProcError, ffmpeg

    width, height = frame
    small = (max(2, width // STILL_SHRINK // 2 * 2), max(2, height // STILL_SHRINK // 2 * 2))
    half = (max(2, width // 4 * 2), max(2, height // 4 * 2))
    dim = f"{STILL_DARKEN:.2f}"
    chain = ",".join(
        (
            f"scale={small[0]}:{small[1]}:flags=area",
            f"gblur=sigma={STILL_SIGMA}",
            f"colorchannelmixer=rr={dim}:gg={dim}:bb={dim}",
            f"scale={half[0]}:{half[1]}:flags=bicubic",
        )
    )
    try:
        ffmpeg(
            [
                "-ss", f"{max(0.0, at):.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", chain, "-q:v", "4", "-update", "1", str(out),
            ],
            timeout=120,
        )
    except (ProcError, OSError, subprocess.TimeoutExpired) as err:
        log.warning("размытый кадр для сцены не снят (%s) — под телефоном будет ровный фон", err)
        return False
    return out.is_file()


def _glyph_fallback(item: dict[str, Any]) -> float:
    rect = item.get("rect") or {}
    return float(
        math.floor(
            min(
                GLYPH_MAX_PX,
                float(rect.get("width", 0)) / GLYPH_BOX[0],
                float(rect.get("height", 0)) / GLYPH_BOX[1],
            )
        )
    )


def stage_visual_assets(
    plan: RenderPlan, assets_dir: str | Path, cfg: Settings | None = None
) -> None:
    """Переносит файлы графики в папку задачи и переписывает пути на имена в ней.

    Шов в `render.prepare_assets`. Работает по `plan.meta["visuals"]` — тому же
    словарю, который уедет рендереру, — и меняет его на месте:

    * картинки стикеров и фото кладутся в папку (жёсткой ссылкой, не вышло —
      копией), в плане остаётся имя;
    * для фона `still` снимается размытый затемнённый кадр из видео плана;
    * если хоть что-то рисует глиф эмодзи, в папку едет цветной шрифт
      (`meta["emoji_font"]`) — как для значков плашек в `prepare_assets`.

    Пропавший файл ролик не роняет: стикер рисуется глифом, фото выпадает,
    фон телефона становится ровным. Повторный вызов ничего не портит.
    """
    cfg = cfg or default_settings
    meta = plan.meta if isinstance(plan.meta, dict) else None
    visuals = meta.get("visuals") if meta is not None else None
    if not isinstance(visuals, dict):
        return
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    glyphs = False

    def keep(item: dict[str, Any]) -> bool:
        """Готовит файл элемента; `False` — элемент остался без картинки и выпадает."""
        nonlocal glyphs
        asset = item.get("asset")
        if asset:
            staged = _stage(asset, assets_dir)
            if staged is not None:
                item["asset"] = staged
            elif item.get("kind") == "sticker" and item.get("emoji"):
                log.warning("картинки стикера %s нет — рисуется глиф", asset)
                item["asset"] = None
                item["glyph_size"] = _glyph_fallback(item)
            else:
                log.warning("картинки %s нет — элемент графики выпал", asset)
                return False
        kind = item.get("kind")
        icon = kind == "chip" and item.get("emoji_box")
        glyph = kind == "sticker" and not item.get("asset")
        if item.get("emoji") and (icon or glyph):
            glyphs = True
        return True

    visuals["overlays"] = [o for o in visuals.get("overlays") or [] if keep(o)]
    for index, scene in enumerate(visuals.get("scenes") or []):
        scene["items"] = [item for item in scene.get("items") or [] if keep(item)]
        if scene.get("heading"):
            keep(scene["heading"])
        bg = scene.get("bg") or {}
        if bg.get("kind") != "still":
            continue
        ready = _stage(bg["asset"], assets_dir) if bg.get("asset") else None
        if ready is not None:
            bg["asset"] = ready
            continue
        video = Path(plan.video)
        if not video.is_file():
            video = assets_dir / plan.video  # видео уже перенесено в папку задачи
        out = assets_dir / f"scene-bg-{index}.jpg"
        middle = (float(scene.get("start", 0.0)) + float(scene.get("end", 0.0))) / 2
        if video.is_file() and _still(video, middle, out, (plan.width, plan.height)):
            bg["asset"] = out.name
        else:
            # Кадра нет — фон остаётся ровным градиентом запасных цветов сцены.
            bg["kind"], bg["asset"] = "gradient", None

    if glyphs and not meta.get("emoji_font"):
        if cfg.emoji_font.exists():
            meta["emoji_font"] = _stage(cfg.emoji_font, assets_dir)
        else:
            log.warning("шрифт эмодзи %s не найден — значки нарисует система", cfg.emoji_font)
