#!/usr/bin/env python3
"""Сборка набора стикеров из Microsoft Fluent Emoji (стиль 3D).

Зачем. Стикер в ролике — вырезанная картинка с белой обводкой, как наклейка.
Глиф цветного шрифта так не выглядит: он плоский и без обводки. Поэтому набор
`assets/stickers/` собирается заранее, а рендерер только показывает готовый PNG
и добавляет тень. Обводка ЗАПЕЧЕНА в файл: считать её фильтром на каждом кадре
дорого, а главное — форма обводки тогда зависела бы от рендерера, которому
решать ничего не положено.

Скрипт, а не разовые команды из истории шелла: набор придётся пересобирать —
добавился ключ в `index.json`, поменялась ширина обводки, — и повторить надо
будет ровно то же самое.

    python tools/make_stickers.py                  # весь набор
    python tools/make_stickers.py --only house key # выборочно
    python tools/make_stickers.py --offline        # только из кеша, без сети

Что делает, по шагам:

1. Читает `assets/stickers/index.json`. Ключи и emoji задаёт он; скрипт их не
   переименовывает и не удаляет, а заполняет только `file`, `aspect`, `source`,
   `license`.
2. Скачивает лицензию репозитория-источника и убеждается, что это MIT. Не MIT —
   останавливается, не скачав ни одной картинки: класть в репозиторий чужое под
   неизвестной лицензией нельзя.
3. Для каждого ключа берёт `metadata.json` папки Fluent и сверяет глиф с emoji
   из индекса. Таблица «ключ → папка» (`FLUENT_DIRS`) набрана руками, и сверка —
   единственное, что ловит опечатку в ней: без неё под ключом `bank` молча
   оказалась бы картинка гостиницы.
4. Скачивает PNG стиля 3D в кеш ВНЕ репозитория. У людей и рук это папка тона
   кожи `Default` (жёлтая, нейтральная).
5. Обрезает по силуэту, масштабирует, строит обводку (`build_sticker`) и пишет
   оптимизированный PNG рядом с индексом.

Сеть: только простые GET без авторизации на raw.githubusercontent.com. Источник
закреплён на конкретном коммите (`SOURCE_REF`), иначе пересборка через год
давала бы другой набор. Недоступен GitHub — другой источник НЕ берётся: у ключа
остаётся `file: null`, и рендерер рисует глиф emoji цветным шрифтом.

Детерминизм: случайности и часов нет; тот же кеш даёт те же пиксели.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
STICKERS_DIR = REPO_ROOT / "assets" / "stickers"
INDEX_PATH = STICKERS_DIR / "index.json"
LICENSE_NAME = "LICENSE-fluentui-emoji.txt"

SOURCE_REPO = "microsoft/fluentui-emoji"
SOURCE_URL = f"https://github.com/{SOURCE_REPO}"
# Коммит ветки main на 2026-11-17. Закреплён, а не «main»: Microsoft набор
# дополняет и перерисовывает, и пересборка обязана давать тот же результат.
SOURCE_REF = "1ffb34c752ecf5d402f04cfb4b392c77f57c54bc"
RAW_BASE = f"https://raw.githubusercontent.com/{SOURCE_REPO}/{SOURCE_REF}"
# Больше скрипту ходить некуда. Проверяется перед КАЖДЫМ запросом: адрес
# собирается из таблицы, и опечатка в ней не должна увести запрос на сторону.
ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com"})

# Кеш скачанного — вне репозитория: исходники Fluent в git не нужны, а
# OneDrive, в котором лежит рабочая копия, синхронизировал бы каждый файл.
CACHE_ENV = "REELBOT_STICKER_CACHE"
DEFAULT_CACHE = (
    Path("C:/Users/Public/reelbot-f5/D/cache")
    if sys.platform == "win32"
    else Path.home() / ".cache" / "reelbot" / "fluentui-emoji"
)

# ---- геометрия стикера. Меняется здесь, а не в рендерере.
SIZE = 512  # длинная сторона файла вместе с обводкой, px
OUTLINE_FRAC = 0.05  # ширина обводки в долях длинной стороны
# Прозрачное поле вокруг обводки: без него сглаженный край упирается в границу
# файла и при повороте стикера в кадре виден срез.
MARGIN = 2
# Силуэт считается на удвоенной сетке и усредняется обратно: край обводки
# получает честное сглаживание, а не лесенку порога.
SUPERSAMPLE = 2
# Что считать телом картинки. Порог низкий: мягкое свечение (✨, 🔥) — тоже
# часть рисунка, и обводка должна обходить его, а не резать.
SOLID_ALPHA = 16
# Радиус скругления ВОГНУТЫХ углов обводки в долях её ширины. Обводка «ровно
# N пикселей от края» повторяет каждую щель рисунка острым клином; у настоящей
# вырубной наклейки нож в щели не заходит. Морфологическое замыкание заливает
# их белым и оставляет контур плавным.
ROUND_FRAC = 0.6

# Ключ набора → папка в `assets/` репозитория Fluent. Имена папок — это
# CLDR-имена emoji; вывести их из самого символа без таблицы CLDR нечем, поэтому
# таблица набрана руками и сверяется по глифу из `metadata.json`.
# `None` — такого emoji во Fluent нет вовсе: `file` остаётся null.
FLUENT_DIRS: dict[str, str | None] = {
    "house": "House",
    "building": "Office building",
    "bank": "Bank",
    "shop": "Convenience store",
    "school": "School",
    "hospital": "Hospital",
    "bricks": "Brick",
    "key": "Key",
    "lock": "Locked",
    "money_bag": "Money bag",
    "banknote": "Dollar banknote",
    "coin": "Coin",
    "card": "Credit card",
    "money_wings": "Money with wings",
    "chart_up": "Chart increasing",
    "chart_down": "Chart decreasing",
    "bar_chart": "Bar chart",
    "briefcase": "Briefcase",
    "handshake": "Handshake",
    "document": "Page facing up",
    "clipboard": "Clipboard",
    "folder": "File folder",
    "scales": "Balance scale",
    "cart": "Shopping cart",
    "bags": "Shopping bags",
    "gift": "Wrapped gift",
    "target": "Bullseye",
    "rocket": "Rocket",
    "fire": "Fire",
    "star": "Star",
    "sparkles": "Sparkles",
    "lightning": "High voltage",
    "hundred": "Hundred points",
    "trophy": "Trophy",
    "medal": "1st place medal",
    "crown": "Crown",
    "gem": "Gem stone",
    "bulb": "Light bulb",
    "brain": "Brain",
    "heart": "Red heart",
    "broken_heart": "Broken heart",
    "muscle": "Flexed biceps",
    "thumbs_up": "Thumbs up",
    "thumbs_down": "Thumbs down",
    "clap": "Clapping hands",
    "eyes": "Eyes",
    "point_down": "Backhand index pointing down",
    "check": "Check mark button",
    "cross": "Cross mark",
    "warning": "Warning",
    "stop": "Stop sign",
    "question": "Red question mark",
    "exclamation": "Red exclamation mark",
    "clock": "Alarm clock",
    "hourglass": "Hourglass not done",
    "calendar": "Calendar",
    "phone": "Mobile phone",
    "laptop": "Laptop",
    "camera": "Camera",
    "clapper": "Clapper board",
    "microphone": "Microphone",
    "headphones": "Headphone",
    "music": "Musical note",
    "tv": "Television",
    "gamepad": "Video game",
    "email": "E-mail",
    "bell": "Bell",
    "megaphone": "Megaphone",
    "magnifier": "Magnifying glass tilted left",
    "link": "Link",
    "gear": "Gear",
    "tools": "Hammer and wrench",
    "hammer": "Hammer",
    "robot": "Robot",
    "books": "Books",
    "pencil": "Pencil",
    "graduation": "Graduation cap",
    "palette": "Artist palette",
    "car": "Automobile",
    "plane": "Airplane",
    "globe": "Globe showing europe-africa",
    "map": "World map",
    "pin": "Round pushpin",
    "sun": "Sun",
    "moon": "Crescent moon",
    "cloud": "Cloud",
    "snowflake": "Snowflake",
    "tree": "Deciduous tree",
    "flower": "Cherry blossom",
    "dog": "Dog face",
    "cat": "Cat face",
    "baby": "Baby",
    # 👪 во Fluent не нарисован: семейные emoji Microsoft в открытый набор не
    # включила (в `assets/` нет ни одной папки Family*).
    "family": None,
    "pill": "Pill",
    "weightlifter": "Person lifting weights",
    "runner": "Person running",
    "pizza": "Pizza",
    "coffee": "Hot beverage",
    "apple": "Red apple",
    "party": "Party popper",
    "thinking": "Thinking face",
    "mind_blown": "Exploding head",
    "cool": "Smiling face with sunglasses",
    "laugh": "Face with tears of joy",
    "cry": "Loudly crying face",
    "love": "Smiling face with heart-eyes",
    "angry": "Pouting face",
    "shock": "Face screaming in fear",
    "money_face": "Money-mouth face",
    "sleep": "Sleeping face",
    "arrow_up": "Up arrow",
    "arrow_down": "Down arrow",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
VARIATION_SELECTOR = "\ufe0f"


class SourceUnavailable(RuntimeError):
    """Файл источника не получен: сети нет, 404 или пришло не то."""


# --------------------------------------------------------------------------- сеть и кеш


def fetch(rel_path: str, cache_dir: Path, *, offline: bool = False) -> bytes:
    """Файл источника по пути внутри репозитория: из кеша, иначе из сети.

    Кеш разложен по коммиту: сменили `SOURCE_REF` — старые файлы не подойдут и
    не подсунутся под новый набор.
    """
    cached = cache_dir / SOURCE_REF / rel_path
    if cached.is_file():
        return cached.read_bytes()
    if offline:
        raise SourceUnavailable(f"{rel_path}: нет в кеше, а сеть запрещена (--offline)")

    url = f"{RAW_BASE}/{urllib.parse.quote(rel_path)}"
    host = urllib.parse.urlsplit(url).hostname
    if host not in ALLOWED_HOSTS:
        raise SourceUnavailable(f"{url}: хост {host!r} не из разрешённых")
    # Клиент называется безлико: имени проекта и машины наружу не отдаём.
    request = urllib.request.Request(url, headers={"User-Agent": "sticker-builder/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as r:  # noqa: S310 — хост проверен
            data = r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceUnavailable(f"{rel_path}: {type(e).__name__}: {e}") from e

    cached.parent.mkdir(parents=True, exist_ok=True)
    # Через временный файл: оборванная запись не должна остаться в кеше под
    # настоящим именем и навсегда подменить собой картинку.
    tmp = cached.with_name(cached.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(cached)
    return data


def fetch_license(cache_dir: Path, *, offline: bool = False) -> str:
    """Текст лицензии источника. Не MIT — исключение: набор собирать нельзя."""
    text = fetch("LICENSE", cache_dir, offline=offline).decode("utf-8")
    flat = " ".join(text.split())
    is_mit = (
        flat.startswith("MIT License")
        and "Copyright (c) Microsoft Corporation" in flat
        and "Permission is hereby granted, free of charge, to any person" in flat
    )
    if not is_mit:
        raise SystemExit(
            f"лицензия {SOURCE_URL} на коммите {SOURCE_REF[:12]} не похожа на MIT — "
            f"набор не собираю. Начало текста: {flat[:120]!r}"
        )
    return text


def asset_path(fluent_dir: str, meta: dict) -> str:
    """Путь PNG стиля 3D внутри репозитория Fluent.

    Имя файла — имя папки строчными с подчёркиваниями (сверено по листингу
    репозитория для всего набора). У emoji с тоном кожи картинки разложены по
    папкам тонов, и признак этого — `unicodeSkintones` в метаданных.
    """
    slug = fluent_dir.lower().replace(" ", "_")
    if meta.get("unicodeSkintones"):
        return f"assets/{fluent_dir}/Default/3D/{slug}_3d_default.png"
    return f"assets/{fluent_dir}/3D/{slug}_3d.png"


def same_emoji(a: str, b: str) -> bool:
    """Один ли это символ. Селектор начертания (U+FE0F) ставят не все и не везде."""
    return a.replace(VARIATION_SELECTOR, "") == b.replace(VARIATION_SELECTOR, "")


def fetch_source(key: str, emoji: str, cache_dir: Path, *, offline: bool = False) -> bytes:
    """Исходный PNG для ключа набора или `SourceUnavailable` с причиной."""
    if key not in FLUENT_DIRS:
        raise SourceUnavailable(f"ключа {key!r} нет в таблице FLUENT_DIRS — допишите папку Fluent")
    fluent_dir = FLUENT_DIRS[key]
    if fluent_dir is None:
        raise SourceUnavailable("во Fluent Emoji такого emoji нет")

    raw_meta = fetch(f"assets/{fluent_dir}/metadata.json", cache_dir, offline=offline)
    try:
        meta = json.loads(raw_meta)
    except ValueError as e:
        raise SourceUnavailable(f"metadata.json папки {fluent_dir!r} не разобрался: {e}") from e
    glyph = str(meta.get("glyph") or "")
    if not same_emoji(glyph, emoji):
        raise SourceUnavailable(
            f"папка {fluent_dir!r} — это {glyph!r}, а в индексе {emoji!r}: поправьте FLUENT_DIRS"
        )

    data = fetch(asset_path(fluent_dir, meta), cache_dir, offline=offline)
    if not data.startswith(PNG_MAGIC):
        raise SourceUnavailable(f"{fluent_dir!r}: пришёл не PNG ({data[:16]!r})")
    return data


# --------------------------------------------------------------------------- обводка


def distance_to(mask: np.ndarray, limit: int) -> np.ndarray:
    """Евклидово расстояние каждого пикселя до ближайшего True, не дальше `limit`.

    Дальше `limit` расстояние не уточняется (вернётся `limit + 1`): обводке
    нужна только полоса вокруг силуэта, а считать всё поле — в разы дольше.

    Два прохода вместо готовой функции: scipy в проекте нет, а ставить
    зависимость ради одной сборочной утилиты незачем. Сначала по строкам —
    расстояние до ближайшего True в своей строке, потом по столбцам — минимум
    суммы квадратов по соседним строкам. Результат точный, а не «по шахматной
    доске»: от этого зависит, круглая ли обводка на углах.

    Считается в целых: расстояния по сетке целые, квадраты малы, и узкий тип
    быстрее float32 просто потому, что вдвое меньше памяти гоняется.
    """
    height, width = mask.shape
    far = limit + 1
    kind = np.uint16 if 2 * far * far < 65_536 else np.int64
    row = np.where(mask, 0, far).astype(kind)
    for x in range(1, width):
        np.minimum(row[:, x], row[:, x - 1] + 1, out=row[:, x])
    for x in range(width - 2, -1, -1):
        np.minimum(row[:, x], row[:, x + 1] + 1, out=row[:, x])
    np.minimum(row, far, out=row)

    # Второй проход — основное время функции. Сдвиг идёт по строкам массива:
    # такие срезы лежат в памяти одним куском (сдвиг по столбцам вдвое
    # медленнее), а сумма пишется в заранее взятый буфер, без новой памяти на
    # каждом шаге.
    sq = row * row
    best = sq.copy()
    buf = np.empty_like(sq)
    for dy in range(1, min(limit, height - 1) + 1):
        shift = kind(dy * dy)
        rest = height - dy
        np.add(sq[:rest], shift, out=buf[:rest])
        np.minimum(best[dy:], buf[:rest], out=best[dy:])
        np.add(sq[dy:], shift, out=buf[:rest])
        np.minimum(best[:rest], buf[:rest], out=best[:rest])
    return np.minimum(np.sqrt(best.astype(np.float32)), np.float32(far))


def _border(free: np.ndarray) -> np.ndarray:
    """Свободные пиксели на краю поля — откуда начинается заливка."""
    seed = np.zeros_like(free)
    seed[0], seed[-1], seed[:, 0], seed[:, -1] = free[0], free[-1], free[:, 0], free[:, -1]
    return seed


def _flood(free: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """Заливка волной: всё свободное, до чего можно дойти от `seed` по соседям."""
    reach = seed & free
    count = int(reach.sum())
    while True:
        wave = reach.copy()
        wave[1:] |= reach[:-1]
        wave[:-1] |= reach[1:]
        wave[:, 1:] |= reach[:, :-1]
        wave[:, :-1] |= reach[:, 1:]
        wave &= free
        grown = int(wave.sum())
        if grown == count:
            return wave
        reach, count = wave, grown


# Сторона клетки грубой сетки для первой прикидки заливки, px.
_COARSE = 8


def outside_of(shape: np.ndarray) -> np.ndarray:
    """Пиксели вне фигуры, до которых можно дойти от края поля.

    Всё остальное «не фигура» — дыры: просвет в ручке кубка, щель между двумя
    звёздами. У наклейки подложка цельная, и дыра в ней, сквозь которую видно
    ролик, читается как брак вырубки, а не как деталь рисунка.

    Волна идёт по пикселю за шаг, и в лоб ей нужно столько шагов, сколько
    пикселей в самом длинном обходе фигуры, — тысяча с лишним. Поэтому сначала
    поле заливается по грубой сетке (клетка свободна, только если свободна
    целиком), а точная волна стартует уже с этой заливки и доделывает кайму
    вдоль фигуры и узкие проходы. Ответ тот же, что у волны с края: грубая
    заливка — подмножество точной, она лишь экономит шаги.
    """
    free = ~shape
    seed = _border(free)
    height, width = free.shape
    if min(height, width) >= 4 * _COARSE:
        # Поле добивается до целого числа клеток НЕсвободными пикселями: клетка
        # с добивкой свободной не считается и ложным началом заливки не станет.
        padded = np.pad(free, ((0, -height % _COARSE), (0, -width % _COARSE)))
        rows, cols = padded.shape[0] // _COARSE, padded.shape[1] // _COARSE
        cells = padded.reshape(rows, _COARSE, cols, _COARSE).all(axis=(1, 3))
        rough = _flood(cells, _border(cells))
        fine = np.repeat(np.repeat(rough, _COARSE, axis=0), _COARSE, axis=1)
        seed |= fine[:height, :width]
    return _flood(free, seed)


@dataclass(slots=True)
class Layout:
    """Размеры стикера: всё, что нужно знать до работы с пикселями."""

    box: tuple[int, int, int, int]  # силуэт в исходнике: left, top, right, bottom
    art: tuple[int, int]  # размер рисунка в стикере
    size: tuple[int, int]  # размер файла
    outline: int  # ширина обводки
    pad: int  # от края файла до рисунка: обводка плюс поле


def layout_of(alpha: np.ndarray) -> Layout:
    """Куда и каким размером ляжет рисунок. Длинная сторона файла — ровно `SIZE`."""
    ys, xs = np.nonzero(alpha > SOLID_ALPHA)
    if not len(xs):
        raise ValueError("картинка целиком прозрачная — обводить нечего")
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    crop_w, crop_h = box[2] - box[0], box[3] - box[1]
    outline = round(SIZE * OUTLINE_FRAC)
    pad = outline + MARGIN
    # Прозрачные поля исходника у разных emoji разные, поэтому масштаб считается
    # от силуэта: иначе стикеры одного размера в кадре выглядели бы разными.
    scale = (SIZE - 2 * pad) / max(crop_w, crop_h)
    art = (max(1, round(crop_w * scale)), max(1, round(crop_h * scale)))
    return Layout(box, art, (art[0] + 2 * pad, art[1] + 2 * pad), outline, pad)


def outline_alpha(solid: Image.Image, lay: Layout) -> np.ndarray:
    """Прозрачность стикера (0..1, размер файла): силуэт, раздутый на обводку.

    `solid` — альфа рисунка, обрезанная по силуэту. Считается на сетке в
    `SUPERSAMPLE` раз плотнее и усредняется: так край получает сглаживание.

    Контур — не просто «всё ближе N px к рисунку», а замыкание этой области
    (раздуть на N + c, сжать на c): выпуклые углы остаются круглыми радиуса N,
    а вогнутые скругляются радиусом c вместо острого клина (`ROUND_FRAC`).
    Дыры внутри контура заливаются (`outside_of`).
    """
    ss = SUPERSAMPLE
    radius = lay.outline * ss
    closing = round(radius * ROUND_FRAC)
    # Рабочее поле шире файла на радиус замыкания: раздутая область выходит за
    # край файла, и сжимать её надо, видя её настоящую границу, а не срез.
    extra = closing + 2
    art_w, art_h = lay.art[0] * ss, lay.art[1] * ss
    big = np.asarray(solid.resize((art_w, art_h), Image.Resampling.BICUBIC))
    off = lay.pad * ss + extra
    mask = np.zeros((lay.size[1] * ss + 2 * extra, lay.size[0] * ss + 2 * extra), dtype=bool)
    mask[off : off + art_h, off : off + art_w] = big > SOLID_ALPHA

    grown = distance_to(mask, radius + closing + 1) <= radius + closing
    outside = outside_of(grown)
    depth = distance_to(outside, closing + 2)  # глубина внутри раздутой области
    soft = np.clip(depth - closing, 0.0, 1.0)[extra:-extra, extra:-extra]
    height, width = lay.size[1], lay.size[0]
    return soft.reshape(height, ss, width, ss).mean(axis=(1, 3))


def build_sticker(source: Image.Image) -> Image.Image:
    """Исходный emoji → стикер: обрезка по силуэту, масштаб, белая обводка.

    Тени нет намеренно: её добавляет рендерер, и запечённая вторая легла бы
    поверх первой.
    """
    source = source.convert("RGBA")
    lay = layout_of(np.asarray(source.getchannel("A")))
    crop = source.crop(lay.box)
    # RGBA Pillow масштабирует в предумноженной альфе: тёмной каймы на краю нет.
    art = np.asarray(crop.resize(lay.art, Image.Resampling.LANCZOS), dtype=np.float32)

    alpha = outline_alpha(crop.getchannel("A"), lay)

    # Рисунок поверх белого. Белым остаётся и всё прозрачное поле: при
    # масштабировании в браузере цвет прозрачных соседей подмешивается в край,
    # и чёрный под нулевой альфой дал бы серую кайму вокруг обводки.
    height, width = lay.size[1], lay.size[0]
    rgb = np.full((height, width, 3), 255.0, dtype=np.float32)
    region = rgb[lay.pad : lay.pad + lay.art[1], lay.pad : lay.pad + lay.art[0]]
    cover = art[..., 3:4] / 255.0
    region[...] = art[..., :3] * cover + 255.0 * (1.0 - cover)

    out = np.empty((height, width, 4), dtype=np.uint8)
    out[..., :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.rint(alpha * 255.0), 0, 255).astype(np.uint8)
    # Под полной прозрачностью цвет ровно белый: и кайме неоткуда взяться, и
    # PNG сжимается лучше, чем с остатками рисунка под нулевой альфой.
    out[out[..., 3] == 0, :3] = 255
    return Image.fromarray(out)  # (h, w, 4) uint8 → RGBA


def encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True, compress_level=9)
    return buf.getvalue()


# --------------------------------------------------------------------------- индекс


def dump_index(index: dict) -> str:
    """Индекс в том же виде, в каком он лежит в репозитории: стикер — строка.

    `json.dumps(indent=...)` развернул бы каждый стикер на четыре строки, и
    любая пересборка давала бы диф на весь файл вместо изменившихся полей.
    """

    def js(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = ["{"]
    for name, value in index.items():
        if name == "stickers":
            continue
        lines.append(f" {js(name)}: {js(value)},")
    lines.append(' "stickers": {')
    entries = [f"  {js(key)}: {js(entry)}" for key, entry in index["stickers"].items()]
    lines.append(",\n".join(entries))
    lines.append(" }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def license_file_text(license_text: str) -> str:
    return (
        "The sticker images in this folder (*.png) are derived from Microsoft Fluent Emoji,\n"
        "3D style.\n"
        "\n"
        f"Source:  {SOURCE_URL}\n"
        f"Commit:  {SOURCE_REF}\n"
        "Changes: cropped to the silhouette, scaled to 512 px on the long side, a white\n"
        "         outline baked around the silhouette (tools/make_stickers.py).\n"
        "\n"
        "Fluent Emoji is Copyright (c) Microsoft Corporation and is used under the MIT\n"
        "License. The license text of the source repository follows unchanged.\n"
        "\n"
        "Стикеры в этой папке сделаны из Microsoft Fluent Emoji (стиль 3D): обрезаны по\n"
        "силуэту, приведены к 512 px и обведены белым. Лицензия источника — MIT, её\n"
        "текст ниже без изменений.\n"
        "\n"
        "--------------------------------------------------------------------------------\n"
        "\n" + license_text.rstrip("\n") + "\n"
    )


# --------------------------------------------------------------------------- сборка


def make_one(
    key: str, emoji: str, cache_dir: Path, *, offline: bool
) -> tuple[bytes, float] | str:
    """PNG стикера и его пропорция — либо строка с причиной, почему его нет."""
    try:
        raw = fetch_source(key, emoji, cache_dir, offline=offline)
        sticker = build_sticker(Image.open(io.BytesIO(raw)))
    except (SourceUnavailable, ValueError, OSError) as e:
        return str(e)
    return encode_png(sticker), round(sticker.width / sticker.height, 4)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Сборка assets/stickers из Microsoft Fluent Emoji (3D) с белой обводкой."
    )
    ap.add_argument("--only", nargs="+", metavar="KEY", help="собрать только эти ключи")
    ap.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get(CACHE_ENV) or DEFAULT_CACHE),
        help=f"папка кеша скачанного, вне репозитория (или переменная {CACHE_ENV})",
    )
    ap.add_argument("--offline", action="store_true", help="сеть не трогать, только кеш")
    ap.add_argument("--jobs", type=int, default=6, help="сколько стикеров собирать разом")
    args = ap.parse_args(argv)

    # Отчёт печатает emoji, а консоль Windows без PYTHONUTF8 — cp1251: на первом
    # же символе вне кодировки скрипт упал бы уже ПОСЛЕ того, как всё собрал.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    cache_dir = args.cache.resolve()
    if cache_dir.is_relative_to(REPO_ROOT):
        raise SystemExit(f"кеш {cache_dir} лежит внутри репозитория — укажите папку вне его")

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    stickers: dict[str, dict] = index["stickers"]
    keys = list(stickers)
    if args.only:
        unknown = sorted(set(args.only) - set(keys))
        if unknown:
            raise SystemExit(f"в индексе нет ключей: {', '.join(unknown)}")
        keys = [k for k in keys if k in set(args.only)]

    try:
        license_text = fetch_license(cache_dir, offline=args.offline)
    except SourceUnavailable as e:
        # Без лицензии картинки не берём: сначала право, потом файлы.
        print(f"источник недоступен ({e}) — набор не тронут", file=sys.stderr)
        return 1

    def work(key: str) -> tuple[str, tuple[bytes, float] | str]:
        return key, make_one(key, stickers[key]["emoji"], cache_dir, offline=args.offline)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = dict(pool.map(work, keys))

    made, missing, total = 0, [], 0
    for key in keys:
        result = results[key]
        entry = stickers[key]
        if isinstance(result, str):
            missing.append((key, result))
            # Прежний файл, если он цел, остаётся: разовый сбой сети не повод
            # разжаловать готовый стикер в глиф.
            if not (entry.get("file") and (STICKERS_DIR / entry["file"]).is_file()):
                entry["file"], entry["aspect"] = None, None
            continue
        data, aspect = result
        name = f"{key}.png"
        (STICKERS_DIR / name).write_bytes(data)
        entry["file"], entry["aspect"] = name, aspect
        made += 1
        total += len(data)

    if any(entry.get("file") for entry in stickers.values()):
        index["source"] = f"Microsoft Fluent Emoji, стиль 3D — {SOURCE_URL} (коммит {SOURCE_REF})"
        index["license"] = f"MIT, Copyright (c) Microsoft Corporation — текст в {LICENSE_NAME}"
        (STICKERS_DIR / LICENSE_NAME).write_text(
            license_file_text(license_text), encoding="utf-8", newline="\n"
        )
    INDEX_PATH.write_text(dump_index(index), encoding="utf-8", newline="\n")

    print(f"собрано {made} из {len(keys)}, {total / 1e6:.2f} МБ")
    for key, why in missing:
        print(f"  без картинки: {key} {stickers[key]['emoji']} — {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
