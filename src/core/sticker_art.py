"""Стикер из картинки с прозрачным фоном: обрезка по силуэту и белая обводка.

Один код на два входа: набор (`tools/make_stickers.py` собирает его из Fluent
Emoji) и стикеры, которые модель дорисовывает под слова ролика
(`pipeline/visuals.py`). Поэтому сгенерированный стикер выглядит в кадре так же,
как стикер набора: та же ширина обводки, те же скруглённые углы.

Случайности и часов нет: те же пиксели на входе дают те же пиксели на выходе.
Зависимости — только numpy и Pillow, они в проекте уже есть.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

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
