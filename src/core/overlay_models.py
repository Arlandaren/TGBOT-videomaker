"""Графика поверх ролика — контракт (плашки, стикеры, декор, сцены).

Два слоя, как у субтитров и вставок:

1. ЛИСТ (`Visuals`, pydantic) — что решил режиссёр: какой приём, к какому слову.
   Только номера слов, секунд здесь нет (§0.1 `edl.py`). Лежит в `EDL.visuals`,
   поэтому перерендер сохранённого листа даёт то же видео (инвариант 4).
2. ПЛАН (`PlanVisuals`, dataclass) — что рисует рендерер: секунды на сетке
   кадров, прямоугольники в пикселях, кегли, цвета. Считает его Python
   (`src/core/overlays.py`), едет он в `plan.meta["visuals"]` — тем же путём,
   что камера (`camera.to_meta`). Зеркало — `render/src/visuals/types.ts`.

Рендерер ничего не решает: ни места, ни времени, ни размера. Ему разрешены
только `transform` и `opacity` на входе и выходе.

Модуль НЕ импортирует `edl.py`: `EDL` импортирует его, и круг здесь недопустим.

Форма движения — часть контракта, потому что Python обязан знать, где окно
сцены в каждом кадре (по нему пересчитывается трек лица):

    smoothstep(t) = t*t*(3 - 2*t),  t зажат в 0..1

    Сцена, кадр f, f0 = round(start*fps), f1 = round(end*fps):
        вход   p = smoothstep((f - f0) / in_frames)
        покой  p = 1
        выход  p = smoothstep((f1 - f) / out_frames)
    то есть выход ЗАКАНЧИВАЕТСЯ в `end`, а не начинается в нём.

    Окно говорящего в кадре f — линейно от полного кадра к `window`:
        x = p*window.x, y = p*window.y,
        width = W + p*(window.width - W), height = H + p*(window.height - H),
        radius = p*radius, tilt = p*tilt
    Видео внутри окна — блок W×H в левом верхнем углу окна с
        transform-origin: 0 0;
        transform: translate(p*tx px, p*ty px) scale(1 + p*(scale - 1))

Элемент (`PlanOverlay`) входит за `in_frames` кадров от `start` и выходит за
`out_frames` кадров, заканчивая выход в `end`. Кривые входа и выхода живут в
`render/src/visuals/motion.mjs`; требования к ним держит тест
(`tests/test_overlay_motion.py`): не короче 8 кадров, перелёт масштаба не больше
4 %, приход ровно в 1.0, прозрачность монотонна.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.geometry import Rect

# --------------------------------------------------------------------------- потолки
#
# Графика — приём, а не фон: когда она стоит на каждом слове, ролик перестаёт
# быть про говорящего. Числа держит валидатор (`overlays.validate_visuals`) и
# знает расстановщик (`pipeline/visuals.py`).

# Плотность поднята по решению владельца 23.11.2026 («плотно, как в
# Инстаграме»): было 8 элементов, один на 4 с, 1.5 с между входами.
OVERLAY_MAX = 16  # элементов на ролик
OVERLAY_WINDOW = 2.5  # не чаще одного элемента на столько секунд ролика
OVERLAY_MIN_GAP = 1.0  # между ВХОДАМИ двух элементов, с
OVERLAY_MAX_VISIBLE = 2  # одновременно в кадре
# Сколько живёт элемент, с: (минимум, максимум) по виду.
OVERLAY_LIFE = {
    "chip": (1.6, 4.0),
    "sticker": (1.2, 3.0),
    "decor": (1.5, 3.5),
}
# Сколько живёт элемент, если конец не задан словом (`until_word_idx is None`).
OVERLAY_DEFAULT_LIFE = {"chip": 2.4, "sticker": 2.0, "decor": 2.4}
CHIP_TEXT_MAX = 28  # знаков в плашке

SCENE_MAX = 3  # сцен на ролик
SCENE_WINDOW = 15.0  # не чаще одной сцены на столько секунд ролика
SCENE_MIN = 2.5  # длина сцены, с
SCENE_MAX_LEN = 6.0
SCENE_MIN_GAP = 6.0  # между сценами, с
SCENE_HEAD = 2.5  # в первые столько секунд ролика сцены нет
SCENE_TAIL = 1.0  # и в последние столько — тоже
SCENE_ITEMS_MAX = 3
SCENE_HEADING_MAX = 32  # знаков в заголовке сцены

# Короче восьми кадров анимация читается как дефект (QUALITY_PLAN Ф6).
MIN_ANIM_FRAMES = 8

OverlayKind = Literal["chip", "sticker", "decor"]
ChipStyle = Literal["pill", "tag", "label"]
DecorGlyph = Literal["question", "exclaim", "sparkle", "arrow"]
Slot = Literal[
    "auto", "above_head", "beside_left", "beside_right", "top_left", "top_right", "chest"
]
SceneLayout = Literal["card_top", "paper", "phone"]
SceneItemKind = Literal["photo", "map", "chip", "sticker"]
# Переход — короткое движение ВИДЕО на склейке или на начале фразы
# (`core/transitions.py`): наезд, въезд сбоку, вспышка, тряска.
TransitionKind = Literal["punch", "slide_left", "slide_right", "flash", "shake"]

TRANSITION_MIN_GAP = 2.0  # между двумя переходами, с
TRANSITION_HEAD = 0.5  # в первые столько секунд ролика перехода нет
TRANSITION_TAIL = 0.6  # и в последние столько — тоже
SHOUT_PUNCH_MAX = 4  # ударных слов с наездом на ролик
SHOUT_PUNCH_GAP = 3.0  # между ними, с


# --------------------------------------------------------------------------- лист


class Overlay(BaseModel):
    """Один элемент поверх кадра: плашка, стикер или декор.

    Все перечисления закрытые — иначе модель изобретёт приём, которого нет в
    рендерере (`edl.py`, шапка). Что обязательно у какого вида:

    - `chip` — `text`. `style`: `pill` — цветная капсула, `tag` — белый ценник
      с наклоном, `label` — мелкая подпись. `emoji` — значок слева, необязателен.
    - `sticker` — `sticker` (ключ набора `assets/stickers/index.json`) ИЛИ
      `emoji` (глиф цветного шрифта, когда картинки в наборе нет).
    - `decor` — `glyph`: россыпь знаков по кадру.
    """

    model_config = ConfigDict(extra="forbid")

    kind: OverlayKind
    # Слово, на котором элемент ПОЯВЛЯЕТСЯ (его начало).
    word_idx: int
    # Слово, на конце которого элемент УХОДИТ. `None` — живёт срок по умолчанию
    # для своего вида (`OVERLAY_LIFE`).
    until_word_idx: int | None = None
    style: ChipStyle = "pill"
    # Текст плашки. Число в нём обязано звучать в речи в этом месте: выдуманная
    # цифра на экране — враньё зрителю, и валидатор такую плашку выбрасывает.
    text: str | None = Field(default=None, max_length=CHIP_TEXT_MAX)
    emoji: str | None = None
    sticker: str | None = None
    # Файл картинки. Заполняет Python (набор стикеров или генерация), не LLM.
    asset: str | None = None
    glyph: DecorGlyph | None = None
    # Куда встать. `auto` — место выбирает Python по свободным зонам кадра.
    slot: Slot = "auto"
    # Зерно раскладки декора и наклона. Ставит Python из позиции в списке:
    # `random` запрещён, перерендер обязан давать то же видео (инвариант 4).
    seed: int = 0


class SceneItem(BaseModel):
    """Содержимое сцены: то, что лежит на доске рядом с окном говорящего."""

    model_config = ConfigDict(extra="forbid")

    kind: SceneItemKind
    # Слово, на котором элемент появляется. Обязано лежать внутри сцены.
    word_idx: int
    # `chip` — текст плашки; `map` — подпись метки; `photo` — подпись под фото.
    text: str | None = Field(default=None, max_length=CHIP_TEXT_MAX)
    sticker: str | None = None
    emoji: str | None = None
    # Заполняет Python, не LLM.
    asset: str | None = None
    asset_aspect: float | None = Field(default=None, gt=0.0)


class Scene(BaseModel):
    """Смена раскладки кадра на несколько секунд.

    - `card_top` — говорящий в скруглённой карточке наверху, под ним доска.
    - `paper` — лист в клетку, говорящий в карточке сбоку, содержимое рядом.
    - `phone` — видео в рамке телефона поверх размытой копии кадра.
    """

    model_config = ConfigDict(extra="forbid")

    layout: SceneLayout
    word_from: int  # сцена начинается на начале этого слова
    word_to: int  # и заканчивается на конце этого
    heading: str | None = Field(default=None, max_length=SCENE_HEADING_MAX)
    items: list[SceneItem] = Field(default_factory=list, max_length=SCENE_ITEMS_MAX)


class Transition(BaseModel):
    """Переход на слове: видео на несколько кадров двигается и встаёт на место.

    `word_idx` — слово, с которого начинается новый кусок (первое после склейки)
    или новая мысль. Момент — начало этого слова; если рядом есть склейка,
    переход прилипает к ней (`transitions.plan_cuts`).
    """

    model_config = ConfigDict(extra="forbid")

    word_idx: int
    kind: TransitionKind


class Visuals(BaseModel):
    """Вся графика ролика. Пустые списки — ролик без графики, как раньше."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    overlays: list[Overlay] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Нет ни элементов, ни сцен. Переходы — не графика поверх кадра, и лист
        с одними переходами для раскладки (`overlays.dressed_plan`) пуст."""
        return not self.overlays and not self.scenes


# --------------------------------------------------------------------------- окна времени
#
# Одно место, где номера слов превращаются в секунды. Им пользуются и правила
# листа (потолки плотности считаются в секундах), и сборка плана: разойдись они
# хоть на кадр, валидатор пропускал бы то, что план потом выбросит.
#
# `window_of(word_idx)` — окно слова на ВЫХОДНОМ таймлайне или `None`, если
# слово вырезано; это `Timeline.word_window`, обёрнутый поиском слова. Функция
# приходит параметром, потому что `edl.py` отсюда импортировать нельзя.

WordWindow = Callable[[int], "tuple[float, float] | None"]


def overlay_window(
    overlay: Overlay, window_of: WordWindow, duration: float
) -> tuple[float, float] | None:
    """Когда элемент в кадре: (начало, конец) в секундах ролика. `None` — выпал.

    Начало — начало слова `word_idx`. Конец — конец слова `until_word_idx`, а
    без него начало плюс срок по умолчанию. Срок зажат в границы своего вида
    (`OVERLAY_LIFE`) и концом ролика. Элемент выпадает, если его слово вырезано
    или до конца ролика не остаётся минимального срока: плашка на полсекунды —
    это вспышка, а не плашка.
    """
    first = window_of(overlay.word_idx)
    if first is None:
        return None
    start = first[0]
    low, high = OVERLAY_LIFE[overlay.kind]
    end = start + OVERLAY_DEFAULT_LIFE[overlay.kind]
    if overlay.until_word_idx is not None:
        last = window_of(overlay.until_word_idx)
        if last is not None and last[1] > start:
            end = last[1]
    end = min(start + high, max(start + low, end), duration)
    if end - start < low - 1e-6:
        return None
    return (start, end)


def scene_window(
    scene: Scene, window_of: WordWindow, duration: float
) -> tuple[float, float] | None:
    """Когда идёт сцена: от начала `word_from` до конца `word_to`. `None` — выпала.

    Сцена не растягивается и не сжимается: смена раскладки привязана к фразе, и
    сдвинуть её границу — значит резать раскладку посреди слова. Поэтому сцена,
    которая не укладывается в `SCENE_MIN..SCENE_MAX_LEN` или залезает в первые
    `SCENE_HEAD` и последние `SCENE_TAIL` секунд, выпадает целиком.
    """
    first = window_of(scene.word_from)
    last = window_of(scene.word_to)
    if first is None or last is None:
        return None
    start, end = first[0], last[1]
    if not (SCENE_MIN - 1e-6 <= end - start <= SCENE_MAX_LEN + 1e-6):
        return None
    if start < SCENE_HEAD - 1e-6 or end > duration - SCENE_TAIL + 1e-6:
        return None
    return (start, end)


# --------------------------------------------------------------------------- план


@dataclass(slots=True)
class PlanMark:
    """Один знак декора: центр в пикселях кадра, кегль, поворот, прозрачность.

    `phase` (0..1) — сдвиг покачивания, чтобы знаки не дышали в унисон.
    """

    x: float
    y: float
    size: float
    rotate: float = 0.0
    opacity: float = 1.0
    phase: float = 0.0


@dataclass(slots=True)
class PlanOverlay:
    """Элемент в том виде, в каком его рисует рендерер. Все числа готовые."""

    kind: str  # chip | sticker | decor | photo | map
    start: float  # секунды на выходном таймлайне, на сетке кадров
    end: float
    in_frames: int
    out_frames: int
    # Место в покое, пиксели кадра. У декора — область россыпи.
    rect: Rect
    rotate: float = 0.0  # наклон в покое, градусы
    anim: str = "pop"  # pop | slide | fade
    layer: str = "front"  # front | behind (за говорящим, нужны маски `behind`)
    seed: int = 0

    # ---- плашка (`chip`): коробка та же, что у плашки пункта (`PlanPill`)
    style: str | None = None  # pill | tag | label | heading
    words: list[str] | None = None  # текст по словам, уже в нужном регистре
    font: str = "caption"  # caption | accent — какой из шрифтов темы
    font_size: float = 0.0
    letter_spacing: float = 0.0  # em
    word_spacing: float = 0.0  # em
    line_height: float = 1.0
    padding_x: float = 0.0
    padding_y: float = 0.0
    radius: float = 0.0
    border_width: float = 0.0
    border_color: str = ""
    fill: str = ""
    fill_opacity: float = 1.0
    color: str = ""  # цвет текста
    emoji: str | None = None  # значок плашки ИЛИ глиф стикера
    emoji_box: float = 0.0
    emoji_size: float = 0.0
    emoji_gap: float = 0.0

    # ---- стикер и фото
    asset: str | None = None  # путь относительно папки ассетов задачи
    glyph_size: float = 0.0  # кегль глифа, когда стикер — эмодзи
    float_px: float = 0.0  # амплитуда покачивания в покое; 0 — стоит

    # ---- декор
    glyph: str | None = None  # question | exclaim | sparkle | arrow
    marks: list[PlanMark] | None = None

    # ---- карта: метка внутри `rect`, пиксели от его левого верхнего угла
    pin: dict[str, float] | None = None

    # Маски «не человек» для `layer == "behind"`: {dir, first, last}.
    behind: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        data["rect"] = self.rect.to_dict()
        return data


@dataclass(slots=True)
class PlanSceneBg:
    """Фон сцены под окном говорящего.

    `paper` — лист в клетку (`grid` — шаг клетки, px), `board` — ровная доска,
    `gradient` — от `color` к `color2` сверху вниз, `still` — заранее размытый
    кадр ролика (`asset`).
    """

    kind: str  # paper | board | gradient | still
    color: str = "#000000"
    color2: str = "#000000"
    grid: float = 0.0
    line_color: str = "#000000"
    asset: str | None = None


@dataclass(slots=True)
class PlanScene:
    """Сцена: куда и как уезжает окно говорящего. Формулы — в шапке модуля."""

    layout: str  # card_top | paper | phone
    start: float
    end: float
    in_frames: int
    out_frames: int
    window: Rect  # окно говорящего в покое
    radius: float  # скругление окна в покое, px
    tilt: float  # наклон окна в покое, градусы
    # Видео внутри окна в покое: сдвиг и масштаб блока W×H.
    scale: float
    tx: float
    ty: float
    bg: PlanSceneBg
    chrome: str = "none"  # none | phone | polaroid
    bezel: float = 0.0  # толщина рамки, px
    bezel_color: str = "#000000"
    heading: PlanOverlay | None = None
    items: list[PlanOverlay] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout": self.layout,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "in_frames": self.in_frames,
            "out_frames": self.out_frames,
            "window": self.window.to_dict(),
            "radius": self.radius,
            "tilt": self.tilt,
            "scale": self.scale,
            "tx": self.tx,
            "ty": self.ty,
            "bg": asdict(self.bg),
            "chrome": self.chrome,
            "bezel": self.bezel,
            "bezel_color": self.bezel_color,
            "heading": self.heading.to_dict() if self.heading else None,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(slots=True)
class PlanVisuals:
    """Графика в плане рендера: значение `plan.meta["visuals"]`."""

    overlays: list[PlanOverlay] = field(default_factory=list)
    scenes: list[PlanScene] = field(default_factory=list)
    version: int = 1

    @property
    def empty(self) -> bool:
        return not self.overlays and not self.scenes

    def to_meta(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "overlays": [o.to_dict() for o in self.overlays],
            "scenes": [s.to_dict() for s in self.scenes],
        }


def smoothstep(t: float) -> float:
    """Та самая кривая сцены. Вторая копия — `render/src/visuals/motion.mjs`."""
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)
