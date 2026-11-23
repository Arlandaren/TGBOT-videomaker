"""Расстановщик графики: какой приём на какое слово (`EDL` → `Visuals`).

Путей два, как у режиссёра монтажа (`director.direct`):

- LLM читает транскрипт и решает смысл: какая цифра заслуживает плашки, какое
  слово просит стикер, где фраза — вопрос, где кадру стоит сменить раскладку.
  Возвращает только номера слов (§0.1 `edl.py`): ни секунд, ни мест в кадре.
- Без LLM — когда нет ключа, выключен флаг или модель дважды ответила негодным
  — работает бесплатный расстановщик: числа из речи, словарь стикеров, вопрос
  и восклицание. Сцен он не ставит: смена раскладки — режиссёрское решение, и
  по словарю его не принять.

Что бы ни вернул любой из путей, результат проходит `sanitize_visuals`: дальше
по конвейеру уезжает только лист, к которому у правил (`core/overlay_rules.py`)
нет вопросов. Наружу шаг не бросает ничего — ролик без графики лучше, чем
ролик, который не собрался из-за украшения.

Случайности здесь нет нигде: один и тот же лист монтажа даёт один и тот же
лист графики (инвариант 4), зёрна раскладки ставятся из позиции в списке.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence, get_args

from pydantic import ValidationError

from src.core.config import settings as default_settings
from src.core.edl import EDL, Word
from src.core.geometry import single_emoji
from src.core.overlay_models import (
    CHIP_TEXT_MAX,
    OVERLAY_DEFAULT_LIFE,
    OVERLAY_MAX,
    OVERLAY_MAX_VISIBLE,
    OVERLAY_MIN_GAP,
    OVERLAY_WINDOW,
    SCENE_HEAD,
    SCENE_HEADING_MAX,
    SCENE_ITEMS_MAX,
    SCENE_MAX,
    SCENE_MAX_LEN,
    SCENE_MIN,
    SCENE_MIN_GAP,
    SCENE_TAIL,
    SCENE_WINDOW,
    ChipStyle,
    DecorGlyph,
    Overlay,
    OverlayKind,
    Scene,
    SceneItem,
    SceneItemKind,
    SceneLayout,
    Slot,
    Transition,
    TransitionKind,
    Visuals,
)
from src.core.overlay_rules import (
    HONESTY_REACH,
    busy_windows,
    check_visuals,
    overlay_cap,
    sanitize_visuals,
    scene_cap,
    sticker_index,
    sticker_keys,
    window_of_edl,
)
from src.llm.client import LLMBadOutput, LLMBudgetExceeded, LLMClient, LLMUnavailable

log = logging.getLogger(__name__)

VISUALS_PROMPT = "plan_visuals"
# Тир называется здесь, а не в `schemas.TIERS`: `LLMClient.complete` получает его
# аргументом и в таблицу не заглядывает — она справочник для тестов реестра.
# `smart`, потому что вопрос тот же, что у `direct`: что в этой фразе главное.
VISUALS_TIER = "smart"

LEXICON_PATH = Path(__file__).with_name("visuals_lexicon.json")


# --------------------------------------------------------------------------- схема ответа


def _values(literal: Any) -> list[str]:
    """Значения закрытого перечисления — прямо из типа листа, без второй копии."""
    return [str(v) for v in get_args(literal)]


def build_schema(stickers: Sequence[str]) -> dict[str, Any]:
    """JSON-схема ответа модели. Только номера слов, времени нет ни в одном поле.

    Перечисления берутся из `Literal`-типов листа, ключи стикеров — из набора:
    модель не может назвать приём, которого нет в рендерере, или картинку,
    которой нет на диске. Потолков КОЛИЧЕСТВА в схеме нет намеренно: лишний
    элемент — перебор, он срезается молча (`sanitize_visuals`), а нарушение
    схемы стоило бы платного ретрая. Потолок есть только там, где его держит
    сама модель листа: три элемента в сцене, длина текста.
    """
    # Без `enum`: схема не в строгом режиме, и ОДИН чужой ключ («telegram»)
    # раньше браковал весь ответ — модель на повторе сдавалась и отдавала
    # пустой лист (прод, 20.11.2026). Набор перечислен в описании, а чужой
    # ключ снимается по одному в `visuals_from_answer`.
    sticker = {
        "type": ["string", "null"],
        "description": (
            "Ключ стикера СТРОГО из набора: " + ", ".join(stickers)
            + ". null — картинки по смыслу нет."
        ),
    }
    emoji = {
        "type": ["string", "null"],
        "description": "РОВНО ОДИН эмодзи или null.",
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["overlays", "scenes"],
        "properties": {
            "transitions": {
                "type": "array",
                "description": (
                    "Переходы: короткое движение видео на склейке или на начале новой "
                    "мысли. Пустой список — программа расставит сама."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["word_idx", "kind"],
                    "properties": {
                        "word_idx": {
                            "type": "integer",
                            "description": "Первое слово нового куска или новой мысли.",
                        },
                        "kind": {"type": "string", "enum": _values(TransitionKind)},
                    },
                },
            },
            "overlays": {
                "type": "array",
                "description": (
                    "Элементы поверх кадра. Пустой список — нормальный ответ: графика "
                    "ставится только там, где она добавляет факт или смысл."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "word_idx"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": _values(OverlayKind),
                            "description": (
                                "`chip` — плашка с фактом (нужен text); `sticker` — "
                                "картинка-наклейка (нужен sticker или emoji); `decor` — "
                                "россыпь знаков по кадру (нужен glyph)."
                            ),
                        },
                        "word_idx": {
                            "type": "integer",
                            "description": "Индекс слова, на котором элемент ПОЯВЛЯЕТСЯ.",
                        },
                        "until_word_idx": {
                            "type": ["integer", "null"],
                            "description": (
                                "Индекс слова, на конце которого элемент УХОДИТ; позже "
                                "word_idx. null — живёт обычный срок для своего вида."
                            ),
                        },
                        "style": {
                            "type": "string",
                            "enum": _values(ChipStyle),
                            "description": "Вид плашки; у стикера и декора не читается.",
                        },
                        "text": {
                            "type": ["string", "null"],
                            "maxLength": CHIP_TEXT_MAX,
                            "description": (
                                "Текст плашки. Каждое число в нём обязано звучать в речи "
                                "рядом с word_idx — цифрами, как в транскрипте."
                            ),
                        },
                        "emoji": emoji,
                        "sticker": sticker,
                        "glyph": {
                            "type": ["string", "null"],
                            "enum": [*_values(DecorGlyph), None],
                            "description": "Знак декора; у плашки и стикера null.",
                        },
                        "slot": {
                            "type": "string",
                            "enum": _values(Slot),
                            "description": "Куда встать. `auto` — место выберет программа.",
                        },
                    },
                },
            },
            "scenes": {
                "type": "array",
                "description": (
                    "Смены раскладки кадра на несколько секунд. Редкий приём: чаще "
                    "всего список пуст."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["layout", "word_from", "word_to"],
                    "properties": {
                        "layout": {"type": "string", "enum": _values(SceneLayout)},
                        "word_from": {
                            "type": "integer",
                            "description": "Сцена начинается на начале этого слова.",
                        },
                        "word_to": {
                            "type": "integer",
                            "description": "И заканчивается на конце этого.",
                        },
                        "heading": {
                            "type": ["string", "null"],
                            "maxLength": SCENE_HEADING_MAX,
                            "description": "Короткий заголовок сцены или null.",
                        },
                        "items": {
                            "type": "array",
                            "maxItems": SCENE_ITEMS_MAX,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["kind", "word_idx"],
                                "properties": {
                                    "kind": {"type": "string", "enum": _values(SceneItemKind)},
                                    "word_idx": {
                                        "type": "integer",
                                        "description": (
                                            "Слово ВНУТРИ сцены, на котором элемент появляется."
                                        ),
                                    },
                                    "text": {
                                        "type": ["string", "null"],
                                        "maxLength": CHIP_TEXT_MAX,
                                        "description": (
                                            "`chip` — текст плашки; `map` — подпись метки; "
                                            "`photo` — подпись под фото."
                                        ),
                                    },
                                    "sticker": sticker,
                                    "emoji": emoji,
                                },
                            },
                        },
                    },
                },
            },
        },
    }


VISUALS_SCHEMA: dict[str, Any] = build_schema(tuple(sticker_index()))


# --------------------------------------------------------------------------- слово и основа


_EDGE_RE = re.compile(r"^[\W_]+|[\W_]+$")


def normalize_word(text: str) -> str:
    """Слово для сравнения: нижний регистр, без краевых знаков, «ё» как «е».

    «ё» ASR ставит через раз, и «плёнку» с «пленку» обязаны быть одним словом.
    Дефис внутри слова остаётся: «чек-лист» — одно слово.
    """
    return _EDGE_RE.sub("", text.lower()).replace("ё", "е")


# Падежные окончания существительного и окончания прилагательного (уже без «ё»).
# Основа совпадает со словом, только если остаток — одно из них: так «дом» берёт
# «дома» и «домами», но не «домен», а «час» — «часов», но не «часть». Простое
# «слово начинается с основы» давало бы стикер кота на каждое «который».
NOUN_ENDINGS = frozenset(
    " а я у ю ы и е о ь й ой ей ою ею ом ем ам ям ах ях ов ев ью ья ье ьи "
    "ами ями ьям ьях ьев ьями s es".split(" ")
)
ADJ_ENDINGS = frozenset(
    "ый ий ой ая яя ое ее ые ие ого его ому ему ым им ых их ую юю ою ею ом ем ей "
    "ыми ими о".split()
)
_MAX_ENDING = max(len(e) for e in NOUN_ENDINGS | ADJ_ENDINGS)


def stem_matches(word: str, stem: str, endings: frozenset[str] = NOUN_ENDINGS) -> bool:
    """Слово — это основа плюс окончание из списка. Только с начала слова."""
    return word.startswith(stem) and word[len(stem):] in endings


def _lookup(word: str, table: Sequence[tuple[str, str]]) -> str:
    """Значение первой основы таблицы, под которую подходит слово; иначе пусто."""
    if not word:
        return ""
    for stem, value in table:
        if stem_matches(word, stem):
            return value
    return ""


# --------------------------------------------------------------------------- словарь стикеров


@dataclass(frozen=True, slots=True)
class Lexicon:
    """Словарь «слово речи → ключ стикера» в разобранном виде."""

    exact: Mapping[str, str]  # слово целиком → ключ
    nouns: Mapping[str, str]  # основа существительного → ключ
    adjectives: Mapping[str, str]  # основа прилагательного → ключ
    skip: frozenset[str]  # слова, которые по основе совпали бы, но значат другое

    def sticker_for(self, text: str) -> str | None:
        """Ключ стикера для слова или `None`.

        Сначала слово целиком, потом основа — от самой длинной: «стартапы» это
        «стартап», а не «старт». Внутри чужого слова основа не ищется вовсе.
        """
        word = normalize_word(text)
        if not word:
            return None
        if word in self.exact:
            return self.exact[word]
        if word in self.skip:
            return None
        for cut in range(len(word), max(0, len(word) - _MAX_ENDING - 1), -1):
            stem, ending = word[:cut], word[cut:]
            if ending in NOUN_ENDINGS and stem in self.nouns:
                return self.nouns[stem]
            if ending in ADJ_ENDINGS and stem in self.adjectives:
                return self.adjectives[stem]
        return None

    def __len__(self) -> int:
        return len(self.exact) + len(self.nouns) + len(self.adjectives)


@lru_cache(maxsize=1)
def load_lexicon() -> Lexicon:
    """Читает `visuals_lexicon.json`. Нет файла или он битый — пустой словарь.

    Словарь — данные рядом с кодом: слова добавляются без правки программы.
    Ключ, которого нет в наборе стикеров, пропускается: звать по нему нечего.
    """
    exact: dict[str, str] = {}
    nouns: dict[str, str] = {}
    adjectives: dict[str, str] = {}
    try:
        data = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
        stickers = data["stickers"]
        if not isinstance(stickers, dict):
            raise ValueError("поле stickers — не объект")
    except (OSError, ValueError, KeyError, TypeError) as err:
        log.warning("словарь стикеров не прочитан (%s) — стикеров по словам не будет", err)
        return Lexicon(exact, nouns, adjectives, frozenset())
    keys = sticker_keys()
    tables = (("exact", exact), ("nouns", nouns), ("adjectives", adjectives))
    for key, entry in stickers.items():
        if key not in keys:
            log.warning("словарь стикеров: ключа %r нет в наборе — пропущен", key)
            continue
        if not isinstance(entry, dict):
            continue
        for name, table in tables:
            for raw in entry.get(name) or []:
                # Первая запись побеждает; повторов в файле нет — это держит тест.
                table.setdefault(normalize_word(str(raw)), key)
    skip = frozenset(normalize_word(str(w)) for w in data.get("skip") or [])
    return Lexicon(exact, nouns, adjectives, skip)


def lexicon_stickers(words: Sequence[Word]) -> list[Overlay]:
    """Все слова, на которые у словаря есть стикер, в порядке речи.

    Это кандидаты, а не расстановка: какие из них встанут в кадр, решает
    `_thin_stickers` — по плотности и по тому, что уже занято плашками.
    """
    lexicon = load_lexicon()
    out: list[Overlay] = []
    for word in words:
        key = lexicon.sticker_for(word.text)
        if key is not None:
            out.append(Overlay(kind="sticker", word_idx=word.idx, sticker=key))
    return out


# --------------------------------------------------------------------------- числа в речи
#
# Плашка с числом — главное, что бесплатный расстановщик умеет: цену, процент
# или срок зритель должен УВИДЕТЬ, на слух они проскакивают. Число берётся из
# транскрипта цифра в цифру (правило честности, `overlay_rules`): «9 500 000
# рублей» становится «9 500 000 ₽», а не «9,5 млн» — пересчёт и округление это
# уже редактура, а не показ сказанного.
#
# Голое число плашкой не становится: «в 2024 году», номер телефона, «глава 3» —
# цифры без единицы ничего не сообщают. Плашку даёт только число с единицей.

_EDGE_PUNCT = ".,…:;!?—–-«»\"“”„'`()[]{}"
_INNER_SPACE = re.compile(r"(?<=\d)[    ](?=\d)")
_NUM_TOKEN = re.compile(
    r"^(?P<pre>[$€£])?(?P<whole>\d+)(?P<frac>[.,]\d+)?(?P<post>%|[$€£₽]|[кk])?$"
)
_CLAUSE_END = re.compile(r"[.,!?…;:][\"»)”]*$")
# Столько цифр подряд в речи — это телефон или номер, а не величина.
PHONE_DIGITS = 10
YEAR_FROM, YEAR_TO = 1900, 2100
_YEAR_WORDS = frozenset("год года году годом годе годы годов годах годам г гг year".split())
_MONEY_SIGNS = frozenset("$€£₽")
# Знак валюты, который пишется ПЕРЕД числом; остальные — после, через пробел.
_SIGN_FIRST = frozenset("$€£")

# Порядок: основа → как это пишется в плашке. Русские множители — сокращением
# через пробел, английские — буквой вплотную («5M»), как их и пишут.
_MAGNITUDES = (
    ("тысяч", " тыс."),
    ("тыс", " тыс."),
    ("миллион", " млн"),
    ("млн", " млн"),
    ("миллиард", " млрд"),
    ("млрд", " млрд"),
    ("триллион", " трлн"),
    ("трлн", " трлн"),
    ("thousand", "K"),
    ("million", "M"),
    ("billion", "B"),
)
# Тенге и гривна остаются словом: их знаков нет в половине шрифтов субтитра.
_CURRENCIES = (
    ("рубл", "₽"),
    ("руб", "₽"),
    ("доллар", "$"),
    ("бакс", "$"),
    ("dollar", "$"),
    ("buck", "$"),
    ("usd", "$"),
    ("евро", "€"),
    ("euro", "€"),
)
# Единицы счёта и меры. Список — данные: слово, после которого число становится
# фактом («5 лет», «300 клиентов»), а не просто цифрой в речи.
_UNIT_STEMS = tuple(
    """
    секунд минут час недел месяц год человек клиент подписчик просмотр заказ заявк
    продаж сделк покупател сотрудник пользовател участник ученик студент отзыв лайк
    комментари шаг урок заняти страниц штук квартир комнат этаж объект метр километр
    сантиметр килограмм грамм литр градус квадрат гектар калори ролик пост стран
    город язык книг компани проект вопрос способ причин правил совет ошибк вариант
    пункт этап second minute hour day week month year user client customer follower
    view step
    """.split()
)
_UNIT_WORDS = frozenset(
    """
    день дня дней дням дни лет сутки суток людей видео сторис сотка сотки соток
    попытка попытки попыток тенге гривен гривны кг км см мм мин сек шт чел people
    """.split()
)
_TIMES_WORDS = frozenset(("раз", "раза", "times"))
# Слово перед числом, которое меняет его смысл: «до 3 млн» и «3 млн» — разные
# факты, и плашка без «до» соврала бы.
_PREFIXES = frozenset(
    "от до около почти более больше менее меньше минимум максимум примерно свыше "
    "over under about".split()
)


@dataclass(frozen=True, slots=True)
class _Num:
    """Числовое слово транскрипта, разобранное на части."""

    whole: str  # целая часть, одни цифры
    frac: str = ""  # дробная часть вместе с разделителем: «,5»
    pre: str = ""  # знак валюты перед числом
    post: str = ""  # знак после числа: %, валюта, «к»

    @property
    def bare(self) -> bool:
        """Одни цифры — только такое слово бывает тройкой в «9 500 000»."""
        return not (self.frac or self.pre or self.post)


def _num_token(text: str) -> _Num | None:
    core = _INNER_SPACE.sub("", text.strip().strip(_EDGE_PUNCT).strip().lower())
    match = _NUM_TOKEN.match(core)
    if match is None:
        return None
    return _Num(
        whole=match["whole"],
        frac=match["frac"] or "",
        pre=match["pre"] or "",
        post=match["post"] or "",
    )


def _year_like(tok: _Num | None) -> bool:
    """Четыре цифры в пределах живой памяти — скорее год, чем величина."""
    return (
        tok is not None
        and tok.bare
        and len(tok.whole) == 4
        and YEAR_FROM <= int(tok.whole) <= YEAR_TO
    )


def _clause_end(text: str) -> bool:
    """Слово закрывает фразу или её часть: единица после него к числу не относится."""
    return bool(_CLAUSE_END.search(text.strip()))


def _group(digits: str) -> str:
    """«9500000» → «9 500 000». Цифры те же, читается с первого взгляда.

    Четырёхзначное число не бьётся — так принято в русском наборе, и «1 500»
    в узкой плашке занимает лишний знак.
    """
    if len(digits) < 5:
        return digits
    head = len(digits) % 3 or 3
    return " ".join([digits[:head], *(digits[k : k + 3] for k in range(head, len(digits), 3))])


def _is_unit(word: str) -> bool:
    return bool(word) and (
        word in _UNIT_WORDS or any(stem_matches(word, stem) for stem in _UNIT_STEMS)
    )


def _chip_text(words: Sequence[Word], toks: Sequence[_Num | None], i: int, j: int) -> str | None:
    """Текст плашки для числа из слов `i..j-1` или `None`, если это не факт.

    `None` значит одно из трёх: цифры без единицы, год-дата, телефон или иная
    россыпь цифр, которая числом не является.
    """
    run = [t for t in toks[i:j] if t is not None]
    first = run[0]
    if len(run) > 1:
        # Несколько числовых слов подряд — одно число, только если это запись
        # тройками: «9», «500», «000». Всё остальное («8 800 555 35 35»,
        # «2 3 раза») — телефон или перечисление.
        triples = all(t.bare and len(t.whole) == 3 for t in run[1:])
        if first.frac or first.post or len(first.whole) > 3 or not triples:
            return None
        number = " ".join(t.whole for t in run)
    else:
        number = _group(first.whole) + first.frac
    if sum(len(t.whole) for t in run) >= PHONE_DIGITS:
        return None

    total = len(words)
    k = j
    # После знака препинания единица к числу уже не относится: «их было 500.
    # Рублей при этом…» — не цена.
    attached = not _clause_end(words[j - 1].text)

    def following() -> str:
        return normalize_word(words[k].text) if attached and k < total else ""

    magnitude = {"к": " тыс.", "k": "K"}.get(first.post, "")
    if not magnitude:
        magnitude = _lookup(following(), _MAGNITUDES)
        if magnitude:
            attached = not _clause_end(words[k].text)
            k += 1

    word = following()
    raw = words[k].text.strip() if attached and k < total else ""
    if len(run) == 1 and not magnitude and _year_like(first) and word in _YEAR_WORDS:
        return None  # «в 2024 году» — дата, а не срок

    currency = first.pre or (first.post if first.post in _MONEY_SIGNS else "")
    if first.post == "%" or raw.startswith("%") or word.startswith("процент") or word == "percent":
        text = f"{number}%"
    elif currency or (currency := _lookup(word, _CURRENCIES)):
        if currency in _SIGN_FIRST:
            text = f"{currency}{number}{magnitude}"
        else:
            text = f"{number}{magnitude} {currency}"
    elif word in _TIMES_WORDS:
        # «в 3 раза» — множитель, «3 раза в неделю» — счёт.
        multiplier = i > 0 and normalize_word(words[i - 1].text) == "в"
        text = f"×{number}" if multiplier else f"{number} {word}"
    elif _is_unit(word):
        text = f"{number}{magnitude} {word}"
    elif magnitude:
        text = f"{number}{magnitude}"
    else:
        return None

    if not text.startswith("×") and i > 0 and not _clause_end(words[i - 1].text):
        prefix = normalize_word(words[i - 1].text)
        if prefix in _PREFIXES and len(prefix) + 1 + len(text) <= CHIP_TEXT_MAX:
            text = f"{prefix} {text}"
    return text if len(text) <= CHIP_TEXT_MAX else None


def number_chips(words: Sequence[Word]) -> list[Overlay]:
    """Плашки для чисел с единицей: цены, проценты, множители, сроки, счёт.

    `words` — слова, оставшиеся в ролике, по порядку. Плашка встаёт на первое
    числовое слово и живёт срок по умолчанию. Одно и то же число дважды не
    показывается: повтор в речи — нажим, повтор на экране — мусор.
    """
    toks = [_num_token(w.text) for w in words]
    out: list[Overlay] = []
    seen: set[str] = set()
    i = 0
    while i < len(words):
        if toks[i] is None:
            i += 1
            continue
        j = i + 1
        while j < len(words) and toks[j] is not None and not _clause_end(words[j - 1].text):
            j += 1
        text = _chip_text(words, toks, i, j)
        if text is None:
            # Россыпь цифр — телефон, код или «2 3 раза»: пропускаем целиком,
            # иначе её хвост («…35 35 минут», «3 раза») вышел бы плашкой.
            # Исключение — год впереди: в «2023 500 человек» второе число
            # настоящее, и после года разбор продолжается со следующего слова.
            i = i + 1 if j - i == 1 or _year_like(toks[i]) else j
            continue
        if text not in seen:
            seen.add(text)
            out.append(Overlay(kind="chip", word_idx=words[i].idx, text=text))
        i = j
    return out


# --------------------------------------------------------------------------- вопрос и восклицание

_SENTENCE_END = re.compile(r"([.!?…]+)[\"»)”]*$")
_QUESTION_WORDS = frozenset(
    "как почему зачем что кто где когда сколько какой какая какие какое чей куда "
    "откуда разве неужели why how what who where when".split()
)
# Слова, которые открывают вопрос и без вопросительного знака. «Что» и «как»
# сюда не входят: в середине речи они союзы.
_STRONG_QUESTION = frozenset("почему зачем неужели разве сколько why".split())
# Короче стольких слов вопрос помечается с первого слова, а не с последнего.
QUESTION_SHORT = 6
EXCLAIM_SHORT = 3


def _sentences(words: Sequence[Word]) -> list[tuple[int, int, str]]:
    """Предложения: (позиция первого слова, позиция последнего, знаки в конце)."""
    out: list[tuple[int, int, str]] = []
    start = 0
    for pos, word in enumerate(words):
        match = _SENTENCE_END.search(word.text.strip())
        if match:
            out.append((start, pos, match.group(1)))
            start = pos + 1
    if start < len(words):
        out.append((start, len(words) - 1, ""))
    return out


def decor_marks(words: Sequence[Word]) -> list[Overlay]:
    """Декор по интонации: не больше одного вопроса и одного восклицания на ролик.

    Вопрос — первая фраза с «?»: знаки появляются на вопросительном слове и
    держатся до конца вопроса. Без знаков вопроса в транскрипте остаётся одна
    примета — фраза открывается словом, которое бывает только вопросом
    («почему», «зачем»). Восклицание — первая фраза с «!».
    """
    out: list[Overlay] = []
    sentences = _sentences(words)

    asked = next((s for s in sentences if "?" in s[2]), None)
    if asked is not None:
        first, last, _ = asked
        anchor = next(
            (p for p in range(first, last + 1) if normalize_word(words[p].text) in _QUESTION_WORDS),
            None,
        )
        if anchor is None:
            anchor = first if last - first < QUESTION_SHORT else last
        out.append(
            Overlay(
                kind="decor",
                glyph="question",
                word_idx=words[anchor].idx,
                until_word_idx=words[last].idx if last > anchor else None,
            )
        )
    else:
        opener = next(
            (s for s in sentences if normalize_word(words[s[0]].text) in _STRONG_QUESTION),
            None,
        )
        if opener is not None:
            out.append(Overlay(kind="decor", glyph="question", word_idx=words[opener[0]].idx))

    shouted = next((s for s in sentences if "!" in s[2] and "?" not in s[2]), None)
    if shouted is not None:
        first, last, _ = shouted
        anchor = first if last - first < EXCLAIM_SHORT else last
        out.append(Overlay(kind="decor", glyph="exclaim", word_idx=words[anchor].idx))
    return out


# --------------------------------------------------------------------------- расстановщик без LLM

# Не чаще одного стикера на столько секунд ролика. Стикер по словарю — догадка
# по одному слову, без смысла фразы; редкий он читается как находка, частый —
# как автозамена.
FALLBACK_STICKER_EVERY = 5.0
_CHIP_STYLES: tuple[ChipStyle, ...] = ("pill", "tag")

# --------------------------------------------------------------------------- переходы без LLM

# Виды по кругу для склеек в одной крупности. `punch` стоит отдельно: он —
# ответ на смену крупности и на начало мысли.
_CUT_KINDS: tuple[TransitionKind, ...] = ("slide_left", "flash", "slide_right", "punch")
# Реже этого переходов на склейках нет — добираем наездом на начале фразы.
FALLBACK_PUNCH_EVERY = 5.0


def cut_words(edl: EDL) -> list[tuple[int, bool]]:
    """Склейки ролика: (первое слово после склейки, сменилась ли крупность).

    Склейка — граница двух оставленных кусков (`edl.kept_cuts`). Слово берётся
    первое, которое звучит в новом куске и осталось в ролике: к нему переход и
    привязывается.
    """
    window_of = window_of_edl(edl)
    kept = sorted(edl.kept_cuts, key=lambda c: c.src_in)
    out: list[tuple[int, bool]] = []
    for prev, cut in zip(kept, kept[1:]):
        first = next(
            (
                w.idx
                for w in edl.words
                if cut.src_in - 1e-6 <= w.start < cut.src_out and window_of(w.idx) is not None
            ),
            None,
        )
        if first is not None:
            out.append((first, prev.framing != cut.framing))
    return out


def fallback_transitions(edl: EDL) -> list[Transition]:
    """Переходы без LLM: на каждой склейке, а где склеек мало — на начале фразы.

    Смена крупности получает наезд (камера там и так прыгает — переход делает
    прыжок движением), остальные склейки — виды по кругу. В ролике без нарезки
    склеек нет вовсе; там начало фразы раз в `FALLBACK_PUNCH_EVERY` секунд
    получает наезд — это и есть «джамп-кат». Частоту окончательно держит
    `transitions.plan_cuts`.
    """
    if getattr(edl, "split_until", None) is not None:
        return []
    window_of = window_of_edl(edl)
    out: list[Transition] = []
    times: list[float] = []
    turn = 0
    for idx, reframed in cut_words(edl):
        window = window_of(idx)
        if window is None:
            continue
        if reframed:
            kind: TransitionKind = "punch"
        else:
            kind = _CUT_KINDS[turn % len(_CUT_KINDS)]
            turn += 1
        out.append(Transition(word_idx=idx, kind=kind))
        times.append(window[0])

    kept = [w for w in edl.words if window_of(w.idx) is not None]
    for first, _, _ in _sentences(kept)[1:]:
        window = window_of(kept[first].idx)
        if window is None:
            continue
        if any(abs(window[0] - t) < FALLBACK_PUNCH_EVERY for t in times):
            continue
        out.append(Transition(word_idx=kept[first].idx, kind="punch"))
        times.append(window[0])
    return sorted(out, key=lambda t: (window_of(t.word_idx) or (0.0, 0.0))[0])


def _thin_stickers(
    stickers: Sequence[Overlay], chips: Sequence[Overlay], edl: EDL
) -> list[Overlay]:
    """Прореживает стикеры словаря до плотности, с которой их можно смотреть.

    Один предмет — один стикер на ролик: третий домик на третье слово «дом» —
    это уже не иллюстрация, а обои. Ключ считается занятым, только когда стикер
    действительно встал: если первое «цель» не прошло по плотности, второе
    вправе получить мишень.
    """
    window_of = window_of_edl(edl)
    duration = edl.timeline().duration
    # Плашки с числами идут первыми и свои места уже заняли; стикерам остаётся
    # то, что не съел потолок ролика.
    cap = overlay_cap(duration)
    limit = min(cap - min(cap, len(chips)), max(1, int(duration // FALLBACK_STICKER_EVERY)))
    if limit <= 0:
        return []
    # В длинном ролике шаг растягивается: иначе все стикеры встали бы в первую
    # минуту, а дальше ролик шёл бы голым.
    every = max(FALLBACK_STICKER_EVERY, duration / (limit + 1))
    chip_starts = [w[0] for chip in chips if (w := window_of(chip.word_idx)) is not None]
    out: list[Overlay] = []
    used: set[str | None] = set()
    last = -every
    for sticker in stickers:
        window = window_of(sticker.word_idx)
        if window is None or sticker.sticker in used:
            continue
        if window[0] - last < every:
            continue
        # Рядом с плашкой стикеру не жить: число важнее, и правила отдадут
        # место ему. Пропускаем сразу, чтобы слот достался следующему слову.
        if any(abs(window[0] - start) < OVERLAY_MIN_GAP for start in chip_starts):
            continue
        out.append(sticker)
        used.add(sticker.sticker)
        last = window[0]
        if len(out) >= limit:
            break
    return out


def _number_seeds(sheet: Visuals) -> Visuals:
    """Зерно раскладки — позиция элемента в списке (`Overlay.seed`).

    Из позиции, а не из `random`: перерендер сохранённого листа обязан дать то
    же видео (инвариант 4), а соседние элементы — разный наклон.
    """
    for position, overlay in enumerate(sheet.overlays):
        overlay.seed = position
    return sheet


def fallback_visuals(edl: EDL) -> Visuals:
    """Графика без LLM: бесплатно, детерминированно, без сети.

    Числа с единицей становятся плашками, слова из словаря — стикерами, вопрос
    и восклицание — декором. Сцен нет: сменить раскладку кадра — решение
    режиссёра, а не словаря. Лист возвращается уже вычищенным
    (`sanitize_visuals`): плашки с числами в приоритете, остальное — что влезло.
    """
    window_of = window_of_edl(edl)
    kept = [w for w in edl.words if window_of(w.idx) is not None]
    if not kept:
        return Visuals()
    chips = number_chips(kept)
    stickers = _thin_stickers(lexicon_stickers(kept), chips, edl)
    sheet = sanitize_visuals(
        Visuals(
            overlays=[*chips, *stickers, *decor_marks(kept)],
            transitions=fallback_transitions(edl),
        ),
        edl,
    )
    # Две одинаковые плашки подряд читаются как таблица; чередование вида
    # считается по уцелевшим, иначе выпавшая плашка сбивала бы шаг.
    turn = 0
    for overlay in sheet.overlays:
        if overlay.kind == "chip":
            overlay.style = _CHIP_STYLES[turn % len(_CHIP_STYLES)]
            turn += 1
    return _number_seeds(sheet)


# --------------------------------------------------------------------------- вызов модели


def timed_words_table(edl: EDL) -> str:
    """Транскрипт для промпта: индекс, секунда ролика, слово, заметная пауза.

    Только слова, оставшиеся после нарезки, и время уже ВЫХОДНОЕ: модели не на
    что сослаться, кроме того, что зритель услышит. Секунды здесь — справка
    для оценки плотности и длины сцены; в ответе их нет (§0.1).
    """
    window_of = window_of_edl(edl)
    kept = [(w, win) for w in edl.words if (win := window_of(w.idx)) is not None]
    lines: list[str] = []
    for n, (word, window) in enumerate(kept):
        gap = ""
        if n + 1 < len(kept):
            pause = kept[n + 1][1][0] - window[1]
            if pause >= 0.35:
                gap = f"   ‹пауза {pause:.1f}с›"
        lines.append(f"{word.idx:4d} {window[0]:6.1f}  {word.text}{gap}")
    return "\n".join(lines)


def _busy_text(edl: EDL) -> str:
    """Занятые места словами транскрипта: «слова 12–18 — вставка».

    Модель думает словами, а не секундами, поэтому окна правил переводятся в
    номера слов, которые в них звучат.
    """
    window_of = window_of_edl(edl)
    spans: list[str] = []
    for label, (start, end) in busy_windows(edl):
        inside = [
            w.idx
            for w in edl.words
            if (win := window_of(w.idx)) is not None and win[0] < end and win[1] > start
        ]
        if inside:
            spans.append(f"слова {min(inside)}–{max(inside)} — {label}")
    return "; ".join(spans) if spans else "нет"


def sticker_table() -> str:
    """Набор стикеров для промпта: ключ и то, что на нём нарисовано."""
    cells = [f"`{key}` {emoji}".rstrip() for key, emoji in sticker_index().items()]
    return "\n".join(", ".join(cells[k : k + 6]) for k in range(0, len(cells), 6))


def prompt_variables(edl: EDL, cfg: Any = None) -> dict[str, str]:
    """Значения для `plan_visuals.md`: потолки этого ролика, занятые места, транскрипт."""
    cfg = cfg or default_settings
    duration = edl.timeline().duration
    scenes = scene_cap(duration, split=getattr(edl, "split_until", None) is not None)
    if duration < SCENE_HEAD + SCENE_MIN + SCENE_TAIL:
        scenes = 0  # сцене физически негде встать: голова, минимум и хвост длиннее ролика
    return {
        "language": getattr(cfg, "whisper_language", "") or "auto",
        "duration": f"{duration:.1f}",
        "max_overlays": str(overlay_cap(duration)),
        "max_scenes": str(scenes),
        "busy": _busy_text(edl),
        "cut_words": ", ".join(str(idx) for idx, _ in cut_words(edl)) or "нет",
        "words_table": timed_words_table(edl),
        # Ниже — постоянные: они одинаковы между роликами и стоят в системной
        # части промпта, поэтому кеш системной части от них не страдает.
        "chip_text_max": str(CHIP_TEXT_MAX),
        "heading_max": str(SCENE_HEADING_MAX),
        "scene_items_max": str(SCENE_ITEMS_MAX),
        "overlay_max": str(OVERLAY_MAX),
        "overlay_window": f"{OVERLAY_WINDOW:g}",
        "overlay_gap": f"{OVERLAY_MIN_GAP:g}",
        "overlay_visible": str(OVERLAY_MAX_VISIBLE),
        "scene_max": str(SCENE_MAX),
        "scene_window": f"{SCENE_WINDOW:g}",
        "scene_min": f"{SCENE_MIN:g}",
        "scene_max_len": f"{SCENE_MAX_LEN:g}",
        "scene_head": f"{SCENE_HEAD:g}",
        "scene_tail": f"{SCENE_TAIL:g}",
        "scene_gap": f"{SCENE_MIN_GAP:g}",
        "honesty_reach": str(HONESTY_REACH),
        "chip_life": f"{OVERLAY_DEFAULT_LIFE['chip']:g}",
        "sticker_life": f"{OVERLAY_DEFAULT_LIFE['sticker']:g}",
        "decor_life": f"{OVERLAY_DEFAULT_LIFE['decor']:g}",
        "stickers": sticker_table(),
    }


def _text(value: Any) -> str | None:
    return (" ".join(value.split()) or None) if isinstance(value, str) else None


def visuals_from_answer(data: Mapping[str, Any]) -> Visuals:
    """Ответ модели → лист графики. Форма уже проверена схемой, смысл — ещё нет.

    В лист едут только поля своего вида: текст у стикера или знак у плашки —
    шум, который дальше пришлось бы обходить. Значения при этом не правятся:
    негодное слово или выдуманное число обязаны дойти до проверки, чтобы о них
    можно было сказать модели.
    """
    overlays: list[Overlay] = []
    for raw in data.get("overlays") or []:
        kind = raw.get("kind")
        fields: dict[str, Any] = {
            "kind": kind,
            "word_idx": raw.get("word_idx"),
            "until_word_idx": raw.get("until_word_idx"),
            "slot": raw.get("slot") or "auto",
        }
        if kind == "chip":
            fields.update(
                text=_text(raw.get("text")),
                style=raw.get("style") or "pill",
                emoji=single_emoji(raw.get("emoji")),
            )
        elif kind == "sticker":
            key = raw.get("sticker")
            if key is not None and key not in sticker_keys():
                # Ключа нет в наборе — рисовать нечем. Эмодзи, если модель его
                # дала, остаётся: стикер нарисуется глифом.
                log.info("графика: стикера %r нет в наборе — снят", key)
                key = None
            fields.update(sticker=key, emoji=single_emoji(raw.get("emoji")))
        elif kind == "decor":
            fields.update(glyph=raw.get("glyph"), slot="auto")
        try:
            overlays.append(Overlay(**fields))
        except ValidationError as err:
            log.info("графика: элемент ответа отброшен (%s): %r", err.errors()[0]["msg"], raw)

    scenes: list[Scene] = []
    for raw in data.get("scenes") or []:
        try:
            items = [
                SceneItem(
                    kind=item.get("kind"),
                    word_idx=item.get("word_idx"),
                    text=_text(item.get("text")),
                    sticker=(
                        item.get("sticker")
                        if item.get("kind") == "sticker" and item.get("sticker") in sticker_keys()
                        else None
                    ),
                    emoji=single_emoji(item.get("emoji")),
                )
                for item in raw.get("items") or []
            ]
            scenes.append(
                Scene(
                    layout=raw.get("layout"),
                    word_from=raw.get("word_from"),
                    word_to=raw.get("word_to"),
                    heading=_text(raw.get("heading")),
                    items=items,
                )
            )
        except ValidationError as err:
            log.info("графика: сцена ответа отброшена (%s): %r", err.errors()[0]["msg"], raw)

    transitions: list[Transition] = []
    for raw in data.get("transitions") or []:
        try:
            transitions.append(Transition(word_idx=raw.get("word_idx"), kind=raw.get("kind")))
        except (ValidationError, AttributeError) as err:
            log.info("графика: переход ответа отброшен (%s): %r", type(err).__name__, raw)
    return Visuals(overlays=overlays, scenes=scenes, transitions=transitions)


def _hollow(scene: Scene) -> bool:
    """Сцена, в которой нечего показывать: доска или лист без единого элемента.

    Раскладка `phone` самодостаточна — в ней показывают само видео. Доска и
    лист в клетку существуют ради содержимого; когда оно выпало (выдуманное
    число, слово вне сцены), пустая доска на три секунды хуже, чем ничего.
    """
    return scene.layout != "phone" and not scene.items and not scene.heading


def _settle(raw: Visuals, edl: EDL) -> Visuals:
    """Чистит лист модели по правилам и снимает сцены, оставшиеся пустыми."""
    sheet = sanitize_visuals(raw, edl)
    if any(_hollow(s) for s in sheet.scenes):
        # Чистим заново от исходных элементов: те, что уступили место пустой
        # сцене, теперь вправе вернуться.
        full = [s for s in sheet.scenes if not _hollow(s)]
        sheet = sanitize_visuals(
            Visuals(overlays=raw.overlays, scenes=full, transitions=raw.transitions), edl
        )
    return _number_seeds(sheet)


# Токенов на символ промпта — с запасом в худшую сторону: кириллица у
# токенизаторов OpenAI идёт по 2.5-3.5 символа на токен, считаем по 2.
CHARS_PER_TOKEN_WORST = 2.0


def worst_cost(
    llm: LLMClient, variables: Mapping[str, Any], correction: str | None, max_tokens: int
) -> float:
    """Цена вызова в худшем случае, $: весь вход без кеша + выход до `max_tokens`."""
    from src.llm.client import load_prompt, render
    from src.llm.providers import price_for

    prompt = load_prompt(VISUALS_PROMPT)
    chars = len(render(prompt.system, dict(variables))) + len(render(prompt.user, dict(variables)))
    chars += len(json.dumps(VISUALS_SCHEMA, ensure_ascii=False)) + len(correction or "")
    miss, _, out = price_for(llm.cfg.model_for_tier(VISUALS_TIER))
    return (chars / CHARS_PER_TOKEN_WORST * miss + max_tokens * out) / 1_000_000


def visuals_llm(cfg: Any = None) -> LLMClient | None:
    """Клиент GPT для графики или `None` — модели нет, работает расстановщик.

    Свой клиент, мимо `llm_provider`: режиссёр остаётся на своём провайдере, а
    графика ходит в OpenAI, только если задан `openai_api_key` и включён
    `visuals_llm`. Модель — `visuals_model` на всех тирах.
    """
    cfg = cfg or default_settings
    key = (getattr(cfg, "openai_api_key", "") or "").strip()
    if not (getattr(cfg, "visuals_enabled", False) and getattr(cfg, "visuals_llm", False) and key):
        return None
    from src.llm.openai_provider import OPENAI_BASE_URL, OpenAIProvider

    model = cfg.visuals_model
    provider = OpenAIProvider(
        key, cfg.openai_base_url or OPENAI_BASE_URL, cfg.llm_timeout_s, thinking=False
    )
    # `model_copy` без проверки: тиры здесь — имена OpenAI, а валидатор
    # конфига требует вендора у моделей шлюза fal, к которому этот клиент
    # отношения не имеет.
    local = cfg.model_copy(
        update={
            "llm_tier_cheap": model,
            "llm_tier_smart": model,
            "llm_tier_vision": model,
            "llm_enabled": True,
        }
    )
    return LLMClient(api_key=key, cfg=local, provider=provider)


async def _ask(edl: EDL, llm: LLMClient, cfg: Any) -> Visuals | None:
    """Лист графики от модели или `None`, если от неё ничего годного не получено.

    Ретрай ровно один (§4) и тратится только на негодный ОТВЕТ: не JSON, не по
    схеме или такой, от которого после правил не осталось ничего из-за ошибок
    самой модели. Частичные огрехи — лишний элемент, выдуманное число у одной
    плашки — ретрая не стоят: негодное срезается, годное остаётся.
    """
    variables = prompt_variables(edl, cfg)
    correction: str | None = None
    cap = float(getattr(cfg, "visuals_max_cost_usd", 0.0) or 0.0)
    max_tokens = int(getattr(cfg, "visuals_max_tokens", 2500) or 2500)
    # Потраченное — по журналу клиента: кривой ответ (LLMBadOutput) тоже
    # оплачен, а до строки после `complete` дело в этом случае не доходит.
    before = len(llm.usages)
    for attempt in (1, 2):
        spent = sum(u.cost_usd for u in llm.usages[before:])
        worst = worst_cost(llm, variables, correction, max_tokens)
        if cap > 0 and spent + worst > cap:
            log.warning(
                "графика: вызов модели не уходит — худший случай $%.5f, потрачено $%.5f, "
                "потолок $%.5f на ролик",
                worst, spent, cap,
            )
            return None
        try:
            result = await llm.complete(
                VISUALS_PROMPT, variables, VISUALS_SCHEMA, VISUALS_TIER,
                correction=correction, max_tokens=max_tokens,
            )
        except LLMUnavailable as err:
            log.warning("графика: LLM недоступна (%s) — бесплатный расстановщик", err)
            return None
        except LLMBudgetExceeded as err:
            # Те же настройки дадут тот же обрыв и вторую оплату (`direct`).
            log.warning("графика: %s — бесплатный расстановщик без повтора", err)
            return None
        except LLMBadOutput as err:
            log.warning("графика: ответ не по схеме (попытка %d): %s", attempt, err)
            if attempt == 2:
                return None
            correction = str(err)
            continue
        except Exception as err:  # noqa: BLE001 — любая осечка провайдера это «модели нет»
            log.warning(
                "графика: вызов модели упал (%s: %s) — бесплатный расстановщик",
                type(err).__name__, err,
            )
            return None

        raw = visuals_from_answer(result.data)
        issues = check_visuals(edl, raw)
        sheet = _settle(raw, edl)
        asked = len(result.data.get("overlays") or []) + len(result.data.get("scenes") or [])
        mistakes = [issue for issue in issues if not issue.excess]
        if asked and sheet.empty and mistakes:
            log.warning(
                "графика: из ответа модели не годится ничего (попытка %d): %s",
                attempt, "; ".join(str(m) for m in mistakes[:6]),
            )
            if attempt == 2:
                return None
            ordered = mistakes + [issue for issue in issues if issue.excess]
            correction = "\n".join(f"- {issue}" for issue in ordered[:10])
            continue

        if issues:
            log.info("графика: срезано по правилам: %s", "; ".join(str(i) for i in issues[:6]))
        log.info(
            "графика: лист от LLM принят с попытки %d (элементов %d, сцен %d, просили %d)",
            attempt, len(sheet.overlays), len(sheet.scenes), asked,
        )
        return sheet
    return None


async def plan_visuals(
    edl: EDL,
    *,
    llm: LLMClient | None = None,
    cfg: Any = None,
    report: dict[str, Any] | None = None,
) -> Visuals:
    """Главный вход шага: лист монтажа → лист графики. Никогда не бросает.

    Модель спрашиваем, только когда она есть, готова и это разрешено флагом
    `visuals_llm` — каждый вызов стоит денег, и по умолчанию их не тратим.
    Пустой ответ модели — тоже ответ: режиссёр решил, что графика этому ролику
    не нужна, и бесплатный расстановщик поверх его решения не запускается.

    `report["from_llm"]` истинно, только когда принят лист модели: вызов мог
    состояться, а лист всё равно собран бесплатным расстановщиком.
    """
    cfg = cfg or default_settings
    if report is not None:
        report["from_llm"] = False
    try:
        if not edl.words:
            return Visuals()
        sheet: Visuals | None = None
        if llm is not None and llm.ready and getattr(cfg, "visuals_llm", False):
            sheet = await _ask(edl, llm, cfg)
            if sheet is not None and report is not None:
                report["from_llm"] = True
        if sheet is None or sheet.empty:
            # Пустой лист модели тоже добирается расстановщиком: на проде
            # (20.11.2026) модель после повтора сдалась пустым ответом, и ролик
            # ушёл без графики вовсе, хотя цены и стикеры в речи были.
            sheet = fallback_visuals(edl)
        elif not sheet.transitions:
            # Модель расставила графику, а переходы оставила программе.
            sheet = sheet.model_copy(update={"transitions": fallback_transitions(edl)})
        # Последняя чистка — гарантия шага, а не недоверие к себе: что бы ни
        # случилось выше, наружу уходит лист, к которому у правил нет вопросов.
        return sanitize_visuals(sheet, edl)
    except Exception:  # noqa: BLE001 — графика не имеет права уронить ролик
        log.exception("графика: расстановка упала — ролик выйдет без графики")
        return Visuals()
