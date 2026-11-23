"""Правила листа графики: что режиссёру можно, а что выпадает.

Лист (`Visuals`) — решение о смысле: какой приём, к какому слову. Здесь живёт
всё, что из этого решения следует арифметикой и на что рендерер уже не
смотрит: существует ли слово, не вырезано ли оно, хватает ли элементу времени,
не слишком ли их много, не врёт ли плашка числом. Геометрии и темы здесь нет —
место и вид считает сборка плана (`core/overlays.py`), а она обязана получить
лист, в котором спорить уже не о чем.

Входов три, и все читают одни и те же правила:

- `validate_visuals` — список нарушений словами. Его текст уезжает модели на
  ретрае (`pipeline/visuals.py`), поэтому каждое сообщение говорит, что менять.
- `sanitize_visuals` — НОВЫЙ лист, из которого всё нарушающее выброшено или
  подрезано. Детерминированно: один и тот же вход даёт один и тот же выход,
  повторный прогон ничего не меняет, а `validate_visuals` на результате пуст.
- `check_visuals` — те же нарушения, но с пометкой «перебор» (см. `Issue`).

Чтобы первые два не разошлись, правило одного элемента записано один раз
(`_examine_overlay`, `_examine_scene`): проверка берёт из него сообщения,
чистка — исправленную копию. Правила плотности считаются по секундам через
`overlay_window` и `scene_window` — те же функции зовёт сборка плана, поэтому
лист, прошедший проверку, план уже не обрежет.

Модуль не импортирует `edl.py` на уровне модуля: тот со временем позовёт
проверку графики из своей, и круг здесь недопустим.
"""

from __future__ import annotations

import json
import logging
import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Collection, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from src.core.config import REPO_ROOT, settings as default_settings
from src.core.geometry import single_emoji
from src.core.overlay_models import (
    OVERLAY_LIFE,
    OVERLAY_MAX,
    OVERLAY_MAX_VISIBLE,
    OVERLAY_MIN_GAP,
    OVERLAY_WINDOW,
    SCENE_HEAD,
    SCENE_ITEMS_MAX,
    SCENE_MAX,
    SCENE_MAX_LEN,
    SCENE_MIN,
    SCENE_MIN_GAP,
    SCENE_TAIL,
    SCENE_WINDOW,
    Overlay,
    Scene,
    SceneItem,
    Transition,
    Visuals,
    WordWindow,
    overlay_window,
    scene_window,
)

if TYPE_CHECKING:  # только для подсказок: см. шапку про круг импортов
    from src.core.edl import EDL

log = logging.getLogger(__name__)

STICKER_INDEX = REPO_ROOT / "assets" / "stickers" / "index.json"

# Допуск сравнения секунд. Окна считаются сложением чисел с плавающей точкой, и
# «ровно 1.5 с между входами» не должно превращаться в нарушение из-за 1e-12.
EPS = 1e-6
# Сколько слов до и после элемента считаются «рядом» для правила честности.
# Три слова — это «за 9 500 000 рублей» вместе с предлогом и единицей: число
# обязано звучать там же, где оно показано, а не где-то в ролике.
HONESTY_REACH = 3

Window = tuple[float, float]


# --------------------------------------------------------------------------- набор стикеров


@lru_cache(maxsize=4)
def _read_sticker_index(stamp: int) -> dict[str, str]:
    """Набор, прочитанный с диска. `stamp` — время правки файла: он и есть ключ кеша."""
    try:
        data = json.loads(STICKER_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        log.warning("набор стикеров не прочитан (%s) — работаем без ключей", err)
        return {}
    stickers = data.get("stickers") if isinstance(data, dict) else None
    if not isinstance(stickers, dict):
        return {}
    return {
        str(key): str(entry.get("emoji") or "") if isinstance(entry, dict) else ""
        for key, entry in stickers.items()
    }


def sticker_index() -> dict[str, str]:
    """Набор стикеров: ключ → эмодзи, в порядке файла. Нет файла — пустой набор.

    Пустой набор не ошибка, а состояние «картинок нет»: стикер по ключу тогда
    не пройдёт проверку, а стикер-эмодзи пройдёт — ролик всё равно соберётся.

    Кеш держится на времени правки файла, как и у сборки плана
    (`overlays.sticker_index`): набор дополняют на живой машине, и проверка
    обязана увидеть новый ключ тогда же, когда его увидит рендер. Возвращается
    копия — чужая правка словаря не должна портить кеш.
    """
    try:
        stamp = STICKER_INDEX.stat().st_mtime_ns
    except OSError:
        return {}
    return dict(_read_sticker_index(stamp))


def sticker_keys() -> frozenset[str]:
    """Ключи, которыми лист вправе звать стикер (`Overlay.sticker`)."""
    return frozenset(sticker_index())


# --------------------------------------------------------------------------- время


def window_of_edl(edl: "EDL") -> WordWindow:
    """Номер слова → его окно на ВЫХОДНОМ таймлайне; `None` — слова нет или оно вырезано.

    Это `Timeline.word_window`, обёрнутый поиском слова, — та самая функция,
    которую ждут `overlay_window` и `scene_window`. Таймлайн строится один раз
    на лист, а не на каждое слово: правила спрашивают одно и то же слово помногу.
    """
    timeline = edl.timeline()
    by_idx = {w.idx: w for w in edl.words}
    cache: dict[int, Window | None] = {}

    def window_of(idx: int) -> Window | None:
        if idx not in cache:
            word = by_idx.get(idx)
            cache[idx] = timeline.word_window(word) if word is not None else None
        return cache[idx]

    return window_of


def overlay_cap(duration: float) -> int:
    """Сколько элементов выдержит ролик такой длины: один на окно, но не больше потолка."""
    return min(OVERLAY_MAX, max(0, math.ceil(duration / OVERLAY_WINDOW)))


def scene_cap(duration: float, *, split: bool = False) -> int:
    """Сколько сцен выдержит ролик. В режиме «Два видео» — ни одной.

    Кадр там уже поделён пополам, и вторая смена раскладки поверх первой даёт
    три окна в кадре вместо одного приёма.
    """
    if split:
        return 0
    return min(SCENE_MAX, max(0, math.ceil(duration / SCENE_WINDOW)))


def busy_windows(edl: "EDL") -> list[tuple[str, Window]]:
    """Что в ролике уже занимает кадр: (причина, окно в секундах).

    На «вставку» и «список» не встаёт сцена; пока кадр «поделён на два видео»,
    не встаёт ничего. Расстановщик показывает эти окна режиссёру заранее —
    словами транскрипта, — чтобы тот не тратил приём на занятое место.
    """
    ruler = _Ruler(edl)
    out: list[tuple[str, Window]] = []
    if ruler.split_until is not None:
        out.append(("кадр поделён на два видео", (0.0, ruler.split_until)))
    out += [("вставка", w) for w in ruler.broll]
    out += [("список", w) for w in ruler.lists]
    return out


def _overlap(a: Window, b: Window) -> bool:
    """Пересекаются ли два окна. Касание встык пересечением не считается."""
    return a[0] < b[1] - EPS and b[0] < a[1] - EPS


def _peak(windows: Sequence[Window]) -> tuple[int, float]:
    """Сколько окон открыто одновременно в худший момент и когда он наступает.

    Одна функция и для проверки, и для чистки: посчитай они «одновременно»
    хоть чуть по-разному, чистка оставляла бы то, что проверка бракует.
    """
    events: list[tuple[float, int]] = []
    for start, end in windows:
        events.append((start, 1))
        events.append((end - EPS, -1))
    # Закрытие раньше открытия в один и тот же момент: окна встык не копятся.
    events.sort(key=lambda ev: (ev[0], ev[1]))
    best, current, at = 0, 0, 0.0
    for moment, delta in events:
        current += delta
        if current > best:
            best, at = current, moment
    return best, at


# --------------------------------------------------------------------------- честность чисел
#
# Число на экране зритель читает как факт. Если в речи его не было, плашка
# врёт — и это хуже отсутствия плашки, поэтому правило не знает исключений:
# каждая группа цифр из текста обязана прозвучать рядом. Сравниваются
# последовательности цифр без пробелов и разделителей: «9 500 000», «9500000»
# и «9.500.000» — одно число, а «11,5» и «11.5» — одно и то же «115».

# Группа цифр: либо число с пробелами между тройками («9 500 000», пробел
# любой — обычный, неразрывный, тонкий), либо сплошные цифры; дробная часть
# через точку или запятую едет вместе с целой. Первая ветка стоит первой
# намеренно: иначе «9 500 000» распалось бы на три группы, и плашка
# «500 000 ₽» прошла бы проверку там, где сказано «9 500 000».
_NUMBER_RE = re.compile(r"(?:\d{1,3}(?:[ \u00a0\u2009\u202f]\d{3})+(?!\d)|\d+)(?:[.,]\d+)*")


def _digits(group: str) -> str:
    # `int(ch)` приводит любые десятичные цифры Юникода к обычным: «٣» и «3» —
    # одна цифра, и подменой алфавита правило не обойти.
    return "".join(str(int(ch)) for ch in group if ch.isdecimal())


def digit_groups(text: str) -> list[str]:
    """Группы цифр текста как строки одних цифр: «от 9 500 000 ₽» → `["9500000"]`."""
    return [_digits(m.group()) for m in _NUMBER_RE.finditer(text or "")]


def invented_numbers(shown: str, said: Collection[str]) -> list[str]:
    """Числа из `shown`, которых нет среди сказанных, — в том виде, как написаны.

    `said` — группы цифр речи (`digit_groups` или `_Ruler.said`).
    """
    return [m.group() for m in _NUMBER_RE.finditer(shown or "") if _digits(m.group()) not in said]


# --------------------------------------------------------------------------- нарушение


@dataclass(frozen=True, slots=True)
class Issue:
    """Одно нарушение правил листа.

    `excess` отделяет перебор от ошибки. Перебор — это когда приём назван
    верно, но ему нет места: элементов больше потолка, два входа рядом, сцена
    короче положенного, до конца ролика не хватает времени. Секунды считает
    программа, а не режиссёр, поэтому перебор срезается молча и ретрая не
    стоит (так же `director.edl_from_plan` снимает лишние плашки и привязки).
    Ошибка — то, что режиссёр мог сделать верно сам: назвал слово, которого
    нет, забыл текст плашки, показал число, которого не было в речи.
    """

    where: str  # «overlays[2]», «scenes[0].items[1]», «overlays»
    message: str
    excess: bool = False

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


# --------------------------------------------------------------------------- что известно о ролике


class _Ruler:
    """Всё, что правилам нужно знать о ролике, посчитанное один раз на лист."""

    def __init__(self, edl: "EDL") -> None:
        self.window_of = window_of_edl(edl)
        self.duration = edl.timeline().duration
        self.words = {w.idx: w for w in edl.words}
        self.keys = sticker_keys()
        # Режим «Два видео»: до этой секунды кадр поделён пополам.
        self.split_until: float | None = getattr(edl, "split_until", None)
        self.split = self.split_until is not None
        self.broll = self._broll_windows(edl)
        self.lists = self._list_windows(edl)
        self.numbers = self._spoken_numbers(edl)

    def _spoken_numbers(self, edl: "EDL") -> list[tuple[int, int, str]]:
        """Числа речи: (первое слово, последнее слово, цифры).

        Число собирается по ВСЕМУ транскрипту, а не по кусочку рядом с
        элементом: иначе граница кусочка резала бы «9 500 000» посередине, и
        плашка «500 000 ₽» находила бы «свои» цифры там, где сказано девять с
        половиной миллионов. Вырезанных слов здесь нет — зритель их не слышит,
        и числу, сказанному только в них, на экране не место.
        """
        parts: list[str] = []
        offsets: list[int] = []
        owners: list[int] = []
        position = 0
        for word in edl.words:
            if self.window_of(word.idx) is None:
                continue
            offsets.append(position)
            owners.append(word.idx)
            parts.append(word.text)
            position += len(word.text) + 1
        out: list[tuple[int, int, str]] = []
        for match in _NUMBER_RE.finditer(" ".join(parts)):
            first = owners[bisect_right(offsets, match.start()) - 1]
            last = owners[bisect_right(offsets, match.end() - 1) - 1]
            out.append((first, last, _digits(match.group())))
        return out

    def said(self, first: int, last: int) -> set[str]:
        """Числа, звучащие рядом с элементом: в словах `first-3 .. last+3`.

        Число считается целиком, если хотя бы одно его слово попало в эти
        границы.
        """
        low, high = first - HONESTY_REACH, last + HONESTY_REACH
        return {digits for a, b, digits in self.numbers if b >= low and a <= high}

    def _broll_windows(self, edl: "EDL") -> list[Window]:
        # Окно вставки считается ровно как в `render_plan.build_plan`: от начала
        # слова на `duration` секунд, но не дальше конца ролика.
        out: list[Window] = []
        for item in getattr(edl, "broll", None) or []:
            window = self.window_of(item.word_idx)
            if window is not None:
                out.append((window[0], min(window[0] + item.duration, self.duration)))
        return out

    def _list_windows(self, edl: "EDL") -> list[Window]:
        # Список плашками стоит в кадре от первого пункта до конца последнего
        # плюс хвост субтитра (`render_plan.lay_list`). Хвост берётся из
        # настроек — там же, где его берёт план: иначе сцена могла бы начаться,
        # пока стопка ещё висит.
        from src.core.edl import list_runs  # здесь, а не наверху: см. шапку модуля

        tail = float(getattr(default_settings, "sub_tail", 0.0) or 0.0)
        captions = getattr(edl, "captions", None) or []
        out: list[Window] = []
        for run in list_runs(captions):
            windows = [
                window
                for bi in run
                for cw in captions[bi].words
                if (window := self.window_of(cw.word_idx)) is not None
            ]
            if windows:
                out.append((min(w[0] for w in windows), max(w[1] for w in windows) + tail))
        return out

    def word_fault(self, name: str, idx: int) -> str | None:
        """Чем плоха ссылка на слово, словами для режиссёра; `None` — слово годится."""
        if idx not in self.words:
            return f"{name}={idx} отсутствует в words — бери индексы только из транскрипта"
        if self.window_of(idx) is None:
            return (
                f"{name}={idx} — это слово вырезано монтажом, привяжи к слову, "
                f"которое осталось в ролике"
            )
        return None


def _clean_text(value: Any) -> str | None:
    """Текст без краевых и сдвоенных пробелов; пустой — `None`."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _plural(n: int, one: str, few: str, many: str) -> str:
    """«1 элемент», «3 элемента», «7 элементов» — сообщения читает и человек."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


# --------------------------------------------------------------------------- правило элемента


def _sticker_fault(
    where: str, sticker: str | None, emoji: str | None, keys: frozenset[str]
) -> tuple[str | None, str | None, list[Issue]]:
    """Ключ и эмодзи стикера, приведённые к правилу, и что в них было не так.

    Стикеру нужен ключ набора ИЛИ один эмодзи. Ключ, которого в наборе нет,
    снимается: рисовать по нему нечего. Эмодзи обязан быть ровно одним цветным
    глифом — он встаёт в окошко под один знак.
    """
    issues: list[Issue] = []
    if sticker is not None and sticker not in keys:
        issues.append(
            Issue(
                where,
                f"sticker={sticker!r} нет в наборе стикеров — возьми ключ из списка "
                f"или поставь emoji",
            )
        )
        sticker = None
    glyph = single_emoji(emoji)
    if glyph is None and (emoji or "").strip():
        issues.append(Issue(where, f"emoji={emoji!r} — нужен ровно ОДИН эмодзи"))
    return sticker, glyph, issues


def _examine_overlay(
    i: int, source: Overlay, ruler: _Ruler
) -> tuple[Overlay | None, Window | None, list[Issue]]:
    """Один элемент против правил: (исправленная копия, окно, нарушения).

    Копия `None` — элемент спасти нельзя, он выпадает целиком. Иначе в копии
    подрезано только то, что нарушало правило; остальное нетронуто.
    """
    where = f"overlays[{i}]"
    issues: list[Issue] = []
    overlay = source.model_copy(deep=True)

    fault = ruler.word_fault("word_idx", overlay.word_idx)
    if fault:
        return None, None, [Issue(where, fault)]

    if overlay.until_word_idx is not None:
        fault = ruler.word_fault("until_word_idx", overlay.until_word_idx)
        if fault is None and overlay.until_word_idx <= overlay.word_idx:
            fault = (
                f"until_word_idx={overlay.until_word_idx} не позже word_idx="
                f"{overlay.word_idx} — элемент уходит ПОСЛЕ того, как появился"
            )
        if fault:
            # Конец не спасает и не губит элемент: без него он живёт срок по
            # умолчанию, а начало названо верно.
            issues.append(Issue(where, fault + "; поставь слово позже или null"))
            overlay.until_word_idx = None

    if overlay.kind == "chip":
        overlay.text = _clean_text(overlay.text)
        if not overlay.text:
            # Длину держит сама модель листа (`max_length`), сюда доходит только
            # пустота: плашка без текста — это цветное пятно.
            issues.append(
                Issue(where, 'плашке (kind="chip") нужен text — допиши текст или убери элемент')
            )
            return None, None, issues
        glyph = single_emoji(overlay.emoji)
        if glyph is None and (overlay.emoji or "").strip():
            issues.append(
                Issue(where, f"emoji={overlay.emoji!r} — значок плашки это ровно ОДИН эмодзи")
            )
        overlay.emoji = glyph
    elif overlay.kind == "sticker":
        overlay.sticker, overlay.emoji, found = _sticker_fault(
            where, overlay.sticker, overlay.emoji, ruler.keys
        )
        issues += found
        if overlay.sticker is None and overlay.emoji is None:
            issues.append(
                Issue(
                    where,
                    'стикеру (kind="sticker") нужен sticker — ключ из набора — или emoji; '
                    "допиши одно из двух или убери элемент",
                )
            )
            return None, None, issues
    elif overlay.kind == "decor":
        if overlay.glyph is None:
            issues.append(
                Issue(where, 'декору (kind="decor") нужен glyph — выбери знак или убери элемент')
            )
            return None, None, issues
    else:  # лист собран в обход модели: такого вида рендерер не знает
        return None, None, [Issue(where, f"kind={overlay.kind!r} — такого вида элемента нет")]

    window = overlay_window(overlay, ruler.window_of, ruler.duration)
    if window is None:
        low = OVERLAY_LIFE[overlay.kind][0]
        issues.append(
            Issue(
                where,
                f"от слова {overlay.word_idx} до конца ролика меньше {low:g} с — элемент "
                f"не успеет прочитаться; поставь раньше или убери",
                excess=True,
            )
        )
        return None, None, issues
    if ruler.split_until is not None and window[0] < ruler.split_until:
        # Пока кадр поделён на два видео, свободному элементу встать некуда:
        # трек лица снят с полного кадра, а голова стоит в нижней половине.
        # Сборка плана такой элемент выбрасывает (`overlays._live_overlays`), и
        # условие здесь то же самое — иначе лист обещал бы то, чего не будет.
        issues.append(
            Issue(
                where,
                f"появляется на {window[0]:.1f} с, а до {ruler.split_until:.1f} с кадр "
                f"поделён на два видео — в это время элементов нет; поставь на слово позже",
                excess=True,
            )
        )
        return None, None, issues

    # «Рядом» — пока элемент в кадре: конец, названный за пределами его срока,
    # не расширяет место, где числу разрешено прозвучать.
    last = overlay.word_idx
    if overlay.until_word_idx is not None:
        for idx in range(overlay.word_idx + 1, overlay.until_word_idx + 1):
            spoken = ruler.window_of(idx)
            if spoken is not None and spoken[0] < window[1] - EPS:
                last = idx
    shown = " ".join(
        part for part in (overlay.text if overlay.kind == "chip" else None, overlay.emoji) if part
    )
    invented = invented_numbers(shown, ruler.said(overlay.word_idx, last))
    if invented:
        issues.append(
            Issue(
                where,
                f"число «{invented[0]}» из «{shown}» не звучит в речи рядом со словом "
                f"{overlay.word_idx} — выдуманная цифра на экране запрещена: возьми число "
                f"из транскрипта как оно сказано или убери элемент",
            )
        )
        return None, None, issues

    return overlay, window, issues


def _scene_time_fault(scene: Scene, ruler: _Ruler) -> str:
    """Почему `scene_window` отказал, словами: длина, начало или конец ролика."""
    first = ruler.window_of(scene.word_from)
    last = ruler.window_of(scene.word_to)
    assert first is not None and last is not None  # слова проверены выше
    start, end = first[0], last[1]
    length = end - start
    if length < SCENE_MIN - EPS:
        return (
            f"длится {length:.1f} с — короче {SCENE_MIN:g} с раскладка не успевает "
            f"встать; раздвинь word_from..word_to или убери сцену"
        )
    if length > SCENE_MAX_LEN + EPS:
        return (
            f"длится {length:.1f} с — дольше {SCENE_MAX_LEN:g} с говорящий теряется; "
            f"сузь word_from..word_to"
        )
    if start < SCENE_HEAD - EPS:
        return (
            f"начинается на {start:.1f} с — в первые {SCENE_HEAD:g} с ролика сцены нет, "
            f"там хук; начни позже"
        )
    return (
        f"кончается за {max(0.0, ruler.duration - end):.1f} с до конца ролика, нужно не "
        f"меньше {SCENE_TAIL:g} с — закончи раньше или убери сцену"
    )


def _examine_item(
    where: str, source: SceneItem, scene: Scene, said: Collection[str], ruler: _Ruler
) -> tuple[SceneItem | None, list[Issue]]:
    """Один элемент сцены против правил: (исправленная копия, нарушения)."""
    issues: list[Issue] = []
    item = source.model_copy(deep=True)

    fault = ruler.word_fault("word_idx", item.word_idx)
    if fault is None and not (scene.word_from <= item.word_idx <= scene.word_to):
        fault = (
            f"word_idx={item.word_idx} вне сцены (слова {scene.word_from}..{scene.word_to}) — "
            f"элемент сцены появляется на слове внутри неё"
        )
    if fault:
        return None, [Issue(where, fault)]

    item.text = _clean_text(item.text)
    if item.kind == "chip":
        if not item.text:
            return None, [Issue(where, 'плашке (kind="chip") нужен text — допиши или убери')]
        item.emoji = single_emoji(item.emoji)
    elif item.kind == "sticker":
        item.sticker, item.emoji, found = _sticker_fault(
            where, item.sticker, item.emoji, ruler.keys
        )
        issues += found
        if item.sticker is None and item.emoji is None:
            issues.append(
                Issue(where, 'стикеру (kind="sticker") нужен sticker из набора или emoji')
            )
            return None, issues
    elif item.kind in ("photo", "map"):
        # Подпись у фото и карты необязательна, картинку подставляет программа.
        item.emoji = single_emoji(item.emoji)
    else:
        return None, [Issue(where, f"kind={item.kind!r} — такого элемента сцены нет")]

    shown = " ".join(part for part in (item.text, item.emoji) if part)
    invented = invented_numbers(shown, said)
    if invented:
        issues.append(
            Issue(
                where,
                f"число «{invented[0]}» из «{shown}» не звучит в речи внутри сцены — "
                f"возьми число из транскрипта как оно сказано или убери элемент",
            )
        )
        return None, issues
    return item, issues


def _examine_scene(
    j: int, source: Scene, items: Sequence[SceneItem], ruler: _Ruler
) -> tuple[Scene | None, Window | None, list[Issue]]:
    """Одна сцена против правил: (исправленная копия, окно, нарушения).

    `items` приходят отдельно от сцены: при чистке их может быть больше, чем
    модель листа разрешает держать в сцене, и лишние отсекаются здесь же —
    после того, как выпали негодные.
    """
    where = f"scenes[{j}]"
    for name in ("word_from", "word_to"):
        fault = ruler.word_fault(name, getattr(source, name))
        if fault:
            return None, None, [Issue(where, fault)]
    if source.word_to < source.word_from:
        return None, None, [
            Issue(
                where,
                f"word_to={source.word_to} раньше word_from={source.word_from} — сцена "
                f"идёт от первого слова к последнему",
            )
        ]

    window = scene_window(source, ruler.window_of, ruler.duration)
    if window is None:
        return None, None, [Issue(where, _scene_time_fault(source, ruler), excess=True)]
    if any(_overlap(window, taken) for taken in ruler.broll):
        return None, None, [
            Issue(
                where,
                "пересекается со вставкой-картинкой — кадр в это время уже занят, "
                "поставь сцену на другие слова",
                excess=True,
            )
        ]
    if any(_overlap(window, taken) for taken in ruler.lists):
        return None, None, [
            Issue(
                where,
                "пересекается со списком из пунктов-плашек — два приёма на одних словах "
                "не ставятся, сдвинь сцену",
                excess=True,
            )
        ]

    issues: list[Issue] = []
    said = ruler.said(source.word_from, source.word_to)
    heading = _clean_text(source.heading)
    if heading is not None:
        invented = invented_numbers(heading, said)
        if invented:
            # Сцена остаётся, уходит только заголовок: раскладка названа верно.
            issues.append(
                Issue(
                    where,
                    f"число «{invented[0]}» из заголовка «{heading}» не звучит в речи "
                    f"внутри сцены — возьми число из транскрипта или убери заголовок",
                )
            )
            heading = None

    kept: list[SceneItem] = []
    for k, item in enumerate(items):
        fixed, found = _examine_item(f"{where}.items[{k}]", item, source, said, ruler)
        issues += found
        if fixed is not None:
            kept.append(fixed)
    if len(kept) > SCENE_ITEMS_MAX:
        issues.append(
            Issue(
                where,
                f"{len(kept)} элементов в сцене, на доске помещается не больше "
                f"{SCENE_ITEMS_MAX} — оставь главные",
                excess=True,
            )
        )
        kept = kept[:SCENE_ITEMS_MAX]

    scene = source.model_copy(deep=True, update={"heading": heading, "items": kept})
    return scene, window, issues


# --------------------------------------------------------------------------- проверка


_FORM_MESSAGES = {
    "missing": "нет обязательного поля",
    "extra_forbidden": "лишнее поле — такого в листе графики нет",
    "int_type": "нужно целое число — индекс слова",
    "int_parsing": "нужно целое число — индекс слова",
    "int_from_float": "нужно целое число — индекс слова",
}


def _form_issues(err: ValidationError) -> list[Issue]:
    """Нарушения ФОРМЫ листа (то, что ловит pydantic) теми же словами, что и правила."""
    out: list[Issue] = []
    for item in err.errors()[:10]:
        where = "visuals"
        for part in item.get("loc") or ():
            where += f"[{part}]" if isinstance(part, int) else f".{part}"
        ctx = item.get("ctx") or {}
        kind = item.get("type", "")
        if kind == "string_too_long":
            message = f"длиннее {ctx.get('max_length')} знаков — сократи"
        elif kind == "too_long":
            message = f"элементов больше {ctx.get('max_length')} — оставь главные"
        elif kind == "literal_error":
            message = f"значение {item.get('input')!r} не из набора: {ctx.get('expected')}"
        else:
            message = _FORM_MESSAGES.get(kind) or str(item.get("msg") or kind)
        out.append(Issue(where, message))
    return out


def _plain(visuals: Any) -> Any:
    """Лист в виде простых данных — чтобы форму проверила сама модель.

    `model_validate` готовому объекту верит на слово, а объект мог быть собран
    в обход проверки (`model_construct`, присваивание поля). Через словарь он
    проверяется заново, каким бы путём ни был получен.
    """
    if isinstance(visuals, BaseModel):
        return visuals.model_dump(warnings=False)
    return visuals


def check_visuals(edl: "EDL", visuals: Any = None) -> list[Issue]:
    """Все нарушения правил листа графики. Пустой список — лист в порядке.

    `visuals` по умолчанию берётся из самого листа монтажа (`edl.visuals`);
    нет его или он пуст — проверять нечего.
    """
    if visuals is None:
        visuals = getattr(edl, "visuals", None)
    if visuals is None:
        return []
    try:
        sheet = Visuals.model_validate(_plain(visuals))
    except ValidationError as err:
        return _form_issues(err)
    if sheet.empty:
        return []

    ruler = _Ruler(edl)
    issues: list[Issue] = []

    # ---- сцены: сначала они, потому что свободным элементам нужны их окна
    scenes: list[tuple[int, Window]] = []
    if sheet.scenes and ruler.split:
        issues.append(
            Issue(
                "scenes",
                "кадр поделён на два видео — сцены в таком ролике не ставятся, "
                "верни пустой список scenes",
                excess=True,
            )
        )
    elif sheet.scenes:
        for j, scene in enumerate(sheet.scenes):
            fixed, window, found = _examine_scene(j, scene, scene.items, ruler)
            issues += found
            if fixed is not None and window is not None:
                scenes.append((j, window))
        cap = scene_cap(ruler.duration)
        if len(sheet.scenes) > cap:
            issues.append(
                Issue(
                    "scenes",
                    f"{len(sheet.scenes)} {_plural(len(sheet.scenes), 'сцена', 'сцены', 'сцен')} "
                    f"при потолке {cap} (1 на {SCENE_WINDOW:g} с, максимум {SCENE_MAX}) — "
                    f"оставь самую нужную",
                    excess=True,
                )
            )
        ordered = sorted(scenes, key=lambda s: (s[1][0], s[0]))
        for (ja, wa), (jb, wb) in zip(ordered, ordered[1:]):
            if wb[0] < wa[1] - EPS:
                issues.append(
                    Issue(
                        f"scenes[{ja}] и scenes[{jb}]",
                        "пересекаются по времени — в кадре одна раскладка, оставь одну сцену",
                        excess=True,
                    )
                )
            elif wb[0] - wa[1] < SCENE_MIN_GAP - EPS:
                issues.append(
                    Issue(
                        f"scenes[{ja}] и scenes[{jb}]",
                        f"между сценами {wb[0] - wa[1]:.1f} с, нужно не меньше "
                        f"{SCENE_MIN_GAP:g} с — разнеси или оставь одну",
                        excess=True,
                    )
                )

    # ---- свободные элементы
    live: list[tuple[int, Overlay, Window]] = []
    for i, overlay in enumerate(sheet.overlays):
        fixed, window, found = _examine_overlay(i, overlay, ruler)
        issues += found
        if fixed is not None and window is not None:
            live.append((i, fixed, window))

    cap = overlay_cap(ruler.duration)
    if len(sheet.overlays) > cap:
        count = len(sheet.overlays)
        issues.append(
            Issue(
                "overlays",
                f"{count} {_plural(count, 'элемент', 'элемента', 'элементов')} при потолке "
                f"{cap} (1 на {OVERLAY_WINDOW:g} с, максимум {OVERLAY_MAX}) — оставь самые "
                f"важные",
                excess=True,
            )
        )
    ordered_live = sorted(live, key=lambda x: (x[2][0], x[0]))
    for (ia, oa, wa), (ib, ob, wb) in zip(ordered_live, ordered_live[1:]):
        if wb[0] - wa[0] < OVERLAY_MIN_GAP - EPS:
            issues.append(
                Issue(
                    f"overlays[{ia}] и overlays[{ib}]",
                    f"входят с разницей {wb[0] - wa[0]:.1f} с (слова {oa.word_idx} и "
                    f"{ob.word_idx}), нужно не меньше {OVERLAY_MIN_GAP:g} с — оставь один "
                    f"или разнеси",
                    excess=True,
                )
            )
    peak, at = _peak([window for _, _, window in live])
    if peak > OVERLAY_MAX_VISIBLE:
        issues.append(
            Issue(
                "overlays",
                f"около {at:.1f} с в кадре одновременно {peak} "
                f"{_plural(peak, 'элемент', 'элемента', 'элементов')}, допустимо не больше "
                f"{OVERLAY_MAX_VISIBLE} — укороти (until_word_idx) или убери лишние",
                excess=True,
            )
        )
    for i, _, window in live:
        inside = next((j for j, taken in scenes if _overlap(window, taken)), None)
        if inside is not None:
            issues.append(
                Issue(
                    f"overlays[{i}]",
                    f"попадает на время сцены scenes[{inside}] — во время сцены свободных "
                    f"элементов нет: перенеси в items сцены или убери",
                    excess=True,
                )
            )
    return issues


def validate_visuals(edl: "EDL", visuals: Any = None) -> list[str]:
    """Семантическая проверка графики. Возвращает список ошибок; пустой — всё в порядке.

    Сообщения написаны для режиссёра: их текст уезжает модели на ретрае, и
    каждое говорит, что именно поменять.
    """
    return [str(issue) for issue in check_visuals(edl, visuals)]


# --------------------------------------------------------------------------- чистка


def _coerce_model(model: type[BaseModel], raw: Any, need: tuple[str, ...]) -> Any:
    """Собирает элемент листа из сырых данных, спасая что можно.

    Лишние ключи срезаются, поле с негодным значением возвращается к значению
    по умолчанию. Не спасти только элемент без опоры (`need`): без вида и слова
    непонятно, что это и где ему стоять.
    """
    if not isinstance(raw, Mapping):
        return None
    data = {key: value for key, value in raw.items() if key in model.model_fields}
    for name in ("text", "heading"):
        if isinstance(data.get(name), str):
            data[name] = _clean_text(data[name])
    try:
        return model.model_validate(data)
    except ValidationError as err:
        bad = {str(item["loc"][0]) for item in err.errors() if item.get("loc")}
    if not bad or bad & set(need):
        return None
    for name in bad:
        data.pop(name, None)
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _coerce(visuals: Any) -> tuple[list[Overlay], list[tuple[Scene, list[SceneItem]]]]:
    """Что угодно → элементы листа, годные по форме. Негодное по форме выпадает."""
    try:
        raw = _plain(visuals)
    except Exception:  # noqa: BLE001 — объект, который нельзя даже прочитать, это пустой лист
        return [], []
    if not isinstance(raw, Mapping):
        return [], []
    overlays = [
        overlay
        for item in _as_list(raw.get("overlays"))
        if (overlay := _coerce_model(Overlay, item, ("kind", "word_idx"))) is not None
    ]
    scenes: list[tuple[Scene, list[SceneItem]]] = []
    for item in _as_list(raw.get("scenes")):
        if not isinstance(item, Mapping):
            continue
        # Сцена собирается без содержимого: его может быть больше, чем держит
        # модель, и какие элементы останутся — решают правила, а не порядок.
        scene = _coerce_model(Scene, {**item, "items": []}, ("layout", "word_from", "word_to"))
        if scene is None:
            continue
        items = [
            scene_item
            for entry in _as_list(item.get("items"))
            if (scene_item := _coerce_model(SceneItem, entry, ("kind", "word_idx"))) is not None
        ]
        scenes.append((scene, items))
    return overlays, scenes


def _rank(overlay: Overlay) -> int:
    """Чем меньше, тем ценнее элемент, когда места на всех не хватает.

    Плашка с числом — факт, ради которого графика и ставится; плашка со словом
    — формулировка режиссёра; стикер — иллюстрация; декор — настроение.
    """
    if overlay.kind == "chip":
        return 0 if digit_groups(overlay.text or "") else 1
    return 2 if overlay.kind == "sticker" else 3


def _trimmed(scene: Scene, ruler: _Ruler) -> Scene:
    """Слишком длинная сцена, подрезанная с конца до `SCENE_MAX_LEN`.

    Модель думает словами и длину в секундах держит плохо: на проде
    (20.11.2026) она дважды из трёх просила сцену на 10-12 с, и раскладка
    выпадала целиком. Начало — это начало мысли, его не трогаем; конец уходит
    на последнее слово, которое ещё укладывается в срок. Короткую, раннюю и
    позднюю сцену это не лечит — они выпадают, как раньше.
    """
    first = ruler.window_of(scene.word_from)
    last = ruler.window_of(scene.word_to)
    if first is None or last is None or last[1] - first[0] <= SCENE_MAX_LEN + EPS:
        return scene
    limit = first[0] + SCENE_MAX_LEN
    fits = [
        idx
        for idx in range(scene.word_from, scene.word_to)
        if (window := ruler.window_of(idx)) is not None and window[1] <= limit + EPS
    ]
    if not fits:
        return scene
    return scene.model_copy(update={"word_to": max(fits)})


def _clean_transitions(raw: Any, ruler: _Ruler) -> list[Transition]:
    """Переходы листа, годные по форме: слово есть и осталось в ролике.

    Один переход на слово, порядок — по времени. Частоту и соседство со
    сценами здесь не судим: это знает только готовый план
    (`transitions.plan_cuts`), а лист обязан пережить повторную чистку без
    изменений.
    """
    if ruler.split:
        return []
    seen: dict[int, tuple[float, Transition]] = {}
    for item in _as_list(raw):
        fixed = _coerce_model(Transition, item, ("word_idx", "kind"))
        if fixed is None or fixed.word_idx in seen:
            continue
        window = ruler.window_of(fixed.word_idx)
        if window is not None:
            seen[fixed.word_idx] = (window[0], fixed)
    return [t for _, t in sorted(seen.values(), key=lambda pair: pair[0])]


def sanitize_visuals(visuals: Any, edl: "EDL") -> Visuals:
    """НОВЫЙ лист графики, в котором нет ни одного нарушения правил.

    Всё, что правило нарушает, выброшено или подрезано; вход не меняется.
    Когда элементов больше, чем ролик выдержит, остаются самые ценные
    (`_rank`), а среди равных — самые ранние: у ролика один смысловой пик, и он
    в начале чаще, чем в конце. Порядок выхода — по времени появления.

    Гарантии, на которых держится конвейер: `validate_visuals(edl, результат)`
    пуст, а повторная чистка результат не меняет.
    """
    overlays, scenes = _coerce(visuals)
    ruler = _Ruler(edl)
    try:
        raw_transitions = _plain(visuals).get("transitions")
    except Exception:  # noqa: BLE001 — нечитаемый лист уже дал пустые списки выше
        raw_transitions = None
    transitions = _clean_transitions(raw_transitions, ruler)
    if not overlays and not scenes:
        return Visuals(transitions=transitions)

    kept_scenes: list[tuple[Scene, Window]] = []
    if not ruler.split:
        candidates: list[tuple[float, int, Scene, Window]] = []
        for j, (scene, items) in enumerate(scenes):
            scene = _trimmed(scene, ruler)
            fixed, window, _ = _examine_scene(j, scene, items, ruler)
            if fixed is not None and window is not None:
                candidates.append((window[0], j, fixed, window))
        candidates.sort(key=lambda c: (c[0], c[1]))
        cap = scene_cap(ruler.duration)
        for _, _, fixed, window in candidates:
            if len(kept_scenes) >= cap:
                break
            # Сцены идут по возрастанию начала, и каждая принятая кончается
            # позже предыдущих — сверяться достаточно с последней.
            if kept_scenes and window[0] - kept_scenes[-1][1][1] < SCENE_MIN_GAP - EPS:
                continue
            kept_scenes.append((fixed, window))
    scene_windows = [window for _, window in kept_scenes]

    pool: list[tuple[int, float, int, Overlay, Window]] = []
    for i, overlay in enumerate(overlays):
        fixed, window, _ = _examine_overlay(i, overlay, ruler)
        if fixed is None or window is None:
            continue
        if any(_overlap(window, taken) for taken in scene_windows):
            continue
        pool.append((_rank(fixed), window[0], i, fixed, window))
    pool.sort(key=lambda c: (c[0], c[1], c[2]))

    cap = overlay_cap(ruler.duration)
    kept: list[tuple[float, int, Overlay, Window]] = []
    for _, start, i, fixed, window in pool:
        if len(kept) >= cap:
            break
        if any(abs(start - other) < OVERLAY_MIN_GAP - EPS for other, _, _, _ in kept):
            continue
        if _peak([w for _, _, _, w in kept] + [window])[0] > OVERLAY_MAX_VISIBLE:
            continue
        kept.append((start, i, fixed, window))
    kept.sort(key=lambda c: (c[0], c[1]))

    return Visuals(
        overlays=[overlay for _, _, overlay, _ in kept],
        scenes=[scene for scene, _ in kept_scenes],
        transitions=transitions,
    )
