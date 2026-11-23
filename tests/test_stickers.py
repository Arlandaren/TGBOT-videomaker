"""Набор стикеров: что лежит в `assets/stickers` и как его собирает утилита.

Две половины. Первая смотрит на готовые файлы — индекс и PNG рядом с ним: это
то, что получит рендерер, и ошибка здесь видна в ролике (чёрный квадрат вместо
прозрачности, стикер без обводки, растянутая картинка). Вторая проверяет саму
сборку (`tools/make_stickers.py`) на нарисованных здесь же фигурах: сеть не
нужна, а предохранитель `conftest` её и не пустил бы.
"""

from __future__ import annotations

import io
import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tools import make_stickers as ms

STICKERS_DIR = Path(__file__).resolve().parents[1] / "assets" / "stickers"
INDEX_PATH = STICKERS_DIR / "index.json"

MAX_SIDE = 512
# «Почти белый»: рисунок под обводкой смешан с белым, и чисто-белой она обязана
# быть только там, куда рисунок не дотягивается. Запас — на округление.
NEAR_WHITE = 245
# Вес всего набора. Он едет в образ и в каждую выкладку.
SET_BUDGET_BYTES = 15 * 1024 * 1024

KEY_RE = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")


def load_index() -> dict:
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def with_files() -> list[tuple[str, dict]]:
    return [(key, entry) for key, entry in load_index()["stickers"].items() if entry["file"]]


@lru_cache(maxsize=None)
def pixels(name: str) -> np.ndarray:
    """RGBA файла набора массивом. Кеш: один файл читают несколько тестов."""
    with Image.open(STICKERS_DIR / name) as im:
        assert im.mode == "RGBA", f"{name}: режим {im.mode}, а нужен RGBA"
        return np.asarray(im).copy()


def near_clear(alpha: np.ndarray, reach: int) -> np.ndarray:
    """Видимые пиксели не дальше `reach` px (по квадрату) от полной прозрачности.

    За краем файла — тоже прозрачность: стикер стоит в кадре без подложки.
    """
    clear = np.pad(alpha == 0, reach, constant_values=True)
    height, width = alpha.shape
    rows = np.zeros((height + 2 * reach, width), dtype=bool)
    for dx in range(2 * reach + 1):
        rows |= clear[:, dx : dx + width]
    near = np.zeros((height, width), dtype=bool)
    for dy in range(2 * reach + 1):
        near |= rows[dy : dy + height]
    return near & (alpha > 0)


# ------------------------------------------------------------------ индекс


def test_index_loads_and_names_its_stickers():
    index = load_index()
    assert index["version"] == 1
    assert isinstance(index["stickers"], dict) and index["stickers"]


def test_keys_are_snake_case_ascii():
    """Ключ пишет модель в лист и читает человек в словаре — без сюрпризов в имени."""
    bad = [key for key in load_index()["stickers"] if not KEY_RE.fullmatch(key)]
    assert not bad, f"ключи не в snake_case ASCII: {bad}"


def test_every_entry_has_an_emoji():
    """Emoji — запасной путь: без файла рендерер рисует глиф, и он обязан быть."""
    for key, entry in load_index()["stickers"].items():
        emoji = entry.get("emoji")
        assert isinstance(emoji, str) and emoji.strip(), f"{key}: нет emoji"
        assert not emoji.isascii(), f"{key}: {emoji!r} — это не emoji"


def test_entries_carry_exactly_the_contract_fields():
    for key, entry in load_index()["stickers"].items():
        assert set(entry) == {"emoji", "file", "aspect"}, f"{key}: поля {sorted(entry)}"
        # Файл и пропорция живут парой: одно без другого рендереру бесполезно.
        assert (entry["file"] is None) == (entry["aspect"] is None), key


def test_set_is_not_empty_and_fits_the_budget():
    files = [entry["file"] for _, entry in with_files()]
    assert files, "в наборе нет ни одной картинки — пересоберите tools/make_stickers.py"
    total = sum((STICKERS_DIR / name).stat().st_size for name in files)
    assert total <= SET_BUDGET_BYTES, f"набор весит {total / 1e6:.1f} МБ"


def test_source_and_license_are_recorded_next_to_the_pictures():
    """Чужие картинки в репозитории — только с названным источником и лицензией."""
    index = load_index()
    assert index["source"] and "fluentui-emoji" in index["source"]
    assert index["license"] and "MIT" in index["license"]
    text = (STICKERS_DIR / ms.LICENSE_NAME).read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Copyright (c) Microsoft Corporation" in text
    assert "Permission is hereby granted, free of charge" in text
    assert ms.SOURCE_REF in text and ms.SOURCE_URL in text


def test_no_stray_pictures_outside_the_index():
    """Файл, которого нет в индексе, никто не покажет — это мусор в образе."""
    listed = {entry["file"] for _, entry in with_files()}
    on_disk = {p.name for p in STICKERS_DIR.glob("*.png")}
    assert on_disk == listed


# ------------------------------------------------------------------ файлы набора


@pytest.mark.parametrize("key,entry", with_files(), ids=[key for key, _ in with_files()])
def test_sticker_file_is_a_cut_out_with_a_white_outline(key: str, entry: dict):
    name = entry["file"]
    assert name == f"{key}.png" and Path(name).name == name, "файл лежит рядом с индексом"
    assert (STICKERS_DIR / name).is_file()

    rgba = pixels(name)
    height, width = rgba.shape[:2]
    alpha = rgba[..., 3]

    assert max(width, height) <= MAX_SIDE
    assert width / height == pytest.approx(entry["aspect"], abs=0.005)

    # Углы прозрачны: непрозрачный угол — это подложка, а не вырезанный стикер.
    corners = [alpha[0, 0], alpha[0, -1], alpha[-1, 0], alpha[-1, -1]]
    assert corners == [0, 0, 0, 0]
    assert (alpha == 255).any(), "в файле нет ни одного непрозрачного пикселя"

    # Обводка: всё видимое у самой границы с прозрачностью — белое, и среди
    # этого есть сплошные пиксели (а не одна полупрозрачная дымка).
    edge = near_clear(alpha, 3)
    assert (alpha[edge] == 255).any(), "у края нет сплошной обводки"
    assert int(rgba[..., :3][edge].min()) >= NEAR_WHITE, "край стикера не белый"

    # И она не волосок: белое держится вглубь на заметную долю размера.
    deep = near_clear(alpha, round(MAX_SIDE * 0.02))
    assert int(rgba[..., :3][deep].min()) >= NEAR_WHITE, "обводка тоньше 2 % размера"

    # Полупрозрачное бывает только на сглаженном крае. Дымка в середине значит,
    # что сквозь стикер просвечивает ролик.
    soft = (alpha > 0) & (alpha < 255)
    assert not (soft & ~edge).any(), "полупрозрачные пиксели внутри стикера"

    # Под нулевой альфой — белый: иначе при масштабировании в браузере цвет
    # прозрачных соседей подмешается в край серой каймой.
    assert int(rgba[..., :3][alpha == 0].min(initial=255)) == 255


def test_every_key_is_mapped_to_a_source_or_marked_absent():
    """Новый ключ в индексе без строки в таблице сборки остался бы без картинки молча."""
    keys = set(load_index()["stickers"])
    assert keys == set(ms.FLUENT_DIRS)


def test_only_keys_the_source_lacks_are_left_without_a_picture():
    """`file: null` допустим только там, где в источнике такого emoji нет."""
    stickers = load_index()["stickers"]
    without_file = {key for key, entry in stickers.items() if entry["file"] is None}
    absent_in_source = {key for key, folder in ms.FLUENT_DIRS.items() if folder is None}
    assert without_file == absent_in_source


# ------------------------------------------------------------------ сборка: обводка


def disk(size: int = 256, radius: int = 90, hole: int = 0) -> Image.Image:
    """Красный круг на прозрачном поле; `hole` — радиус дыры в середине."""
    yy, xx = np.mgrid[0:size, 0:size]
    dist = np.hypot(xx - size / 2 + 0.5, yy - size / 2 + 0.5)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., 0] = 220
    rgba[..., 3] = np.where((dist <= radius) & (dist >= hole), 255, 0)
    return Image.fromarray(rgba)


@pytest.fixture
def small(monkeypatch):
    """Стикер 128 px вместо 512: геометрия задана долями, а считать в 16 раз меньше."""
    # Размер живёт в `src/core/sticker_art.py` — там его читает сборка; имя в
    # `make_stickers` меняем тоже: тесты сверяются с `ms.SIZE`.
    from src.core import sticker_art

    monkeypatch.setattr(sticker_art, "SIZE", 128)
    monkeypatch.setattr(ms, "SIZE", 128)


def test_outline_is_about_five_percent_of_the_long_side():
    out = np.asarray(ms.build_sticker(disk()))
    height, width = out.shape[:2]
    assert max(width, height) == ms.SIZE

    row = out[height // 2]
    visible = np.nonzero(row[:, 3] > 127)[0]
    art = np.nonzero((row[:, 3] == 255) & (row[:, 1] < 128))[0]  # красное: зелёного мало
    left = art[0] - visible[0]
    right = visible[-1] - art[-1]
    want = ms.SIZE * ms.OUTLINE_FRAC
    assert left == pytest.approx(want, abs=2.5) and right == pytest.approx(want, abs=2.5)
    # Поле вокруг обводки прозрачно: сглаженный край не упирается в границу файла.
    assert visible[0] >= 1 and visible[-1] <= width - 2
    assert (out[0, :, 3] == 0).all() and (out[:, 0, 3] == 0).all()


def test_outline_follows_the_silhouette_not_the_source_canvas(small):
    """Поля исходника у разных emoji разные — размер стикера от них не зависит."""
    small = ms.build_sticker(disk(radius=40))
    large = ms.build_sticker(disk(radius=110))
    assert small.size == large.size == (ms.SIZE, ms.SIZE)


def test_wide_picture_keeps_its_proportions(small):
    wide = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    wide.paste((30, 90, 200, 255), (28, 100, 228, 150))  # 200×50
    out = ms.build_sticker(wide)
    assert out.width == ms.SIZE
    pad = round(ms.SIZE * ms.OUTLINE_FRAC) + ms.MARGIN
    assert (out.height - 2 * pad) / (out.width - 2 * pad) == pytest.approx(0.25, abs=0.01)


def test_holes_in_the_backing_are_filled_white(small):
    """Подложка наклейки цельная: сквозь дыру в ней был бы виден ролик."""
    out = np.asarray(ms.build_sticker(disk(radius=110, hole=80)))
    centre = out[out.shape[0] // 2, out.shape[1] // 2]
    assert centre.tolist() == [255, 255, 255, 255]


def test_soft_glow_counts_as_part_of_the_picture():
    """Свечение — часть рисунка: обводка обходит его, а не режет по ядру."""
    glow = np.asarray(disk(radius=60)).copy()
    yy, xx = np.mgrid[0:256, 0:256]
    dist = np.hypot(xx - 127.5, yy - 127.5)
    glow[(dist > 60) & (dist <= 100), 3] = 60  # дымка вокруг ядра
    core_only = ms.layout_of(np.asarray(disk(radius=60))[..., 3])
    with_glow = ms.layout_of(glow[..., 3])
    assert with_glow.box[2] - with_glow.box[0] > core_only.box[2] - core_only.box[0]


def test_fully_transparent_source_is_refused():
    with pytest.raises(ValueError):
        ms.build_sticker(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))


def test_build_is_deterministic(small):
    """Тот же исходник — те же байты: пересборка не должна давать диф в git."""
    first = ms.encode_png(ms.build_sticker(disk()))
    second = ms.encode_png(ms.build_sticker(disk()))
    assert first == second


def test_distance_is_euclidean_not_chessboard():
    """От этого зависит, круглая ли обводка на углах."""
    mask = np.zeros((41, 41), dtype=bool)
    mask[20, 20] = True
    dist = ms.distance_to(mask, 30)
    assert dist[20, 30] == pytest.approx(10.0)
    assert dist[23, 24] == pytest.approx(5.0)
    assert dist[30, 30] == pytest.approx(200**0.5, abs=1e-4)
    assert dist[0, 0] == pytest.approx(800**0.5, abs=1e-4)
    # Дальше лимита расстояние не уточняется.
    assert ms.distance_to(mask, 5)[20, 40] == pytest.approx(6.0)


def test_coarse_shortcut_floods_exactly_what_the_plain_wave_does():
    """Грубая сетка только экономит шаги: дыры и узкие проходы решает точная волна."""
    shape = np.zeros((120, 150), dtype=bool)
    shape[20:100, 20:130] = True
    shape[40:80, 40:70] = False  # дыра: наружу хода нет
    shape[40:80, 90:110] = False  # карман...
    shape[58:60, 110:130] = False  # ...с проходом наружу в два пикселя
    plain = ms._flood(~shape, ms._border(~shape))
    fast = ms.outside_of(shape)

    assert np.array_equal(fast, plain)
    assert not fast[60, 55], "дыра осталась дырой"
    assert fast[60, 100], "карман соединён с краем узким проходом"


# ------------------------------------------------------------------ сборка: индекс и источник


def test_index_dump_keeps_the_committed_format():
    """Пересборка меняет поля, а не вёрстку файла: диф остаётся читаемым."""
    text = INDEX_PATH.read_text(encoding="utf-8")
    assert ms.dump_index(json.loads(text)) == text


def test_variation_selector_does_not_split_one_emoji_in_two():
    assert ms.same_emoji("❤️", "❤")
    assert ms.same_emoji("⚠", "⚠️")
    assert not ms.same_emoji("🏦", "🏨")


def test_people_and_hands_come_from_the_default_skin_tone_folder():
    plain = ms.asset_path("Money bag", {"glyph": "💰"})
    toned = ms.asset_path("Thumbs up", {"glyph": "👍", "unicodeSkintones": ["1f44d 1f3fb"]})
    assert plain == "assets/Money bag/3D/money_bag_3d.png"
    assert toned == "assets/Thumbs up/Default/3D/thumbs_up_3d_default.png"


def test_source_is_pinned_to_a_commit():
    """«main» через год — другой набор; пересборка обязана давать тот же."""
    assert re.fullmatch(r"[0-9a-f]{40}", ms.SOURCE_REF)
    assert ms.SOURCE_REF in ms.RAW_BASE
    assert ms.ALLOWED_HOSTS == {"raw.githubusercontent.com"}


def seed_cache(cache: Path, rel_path: str, data: bytes) -> None:
    path = cache / ms.SOURCE_REF / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


MIT_TEXT = (
    "    MIT License\n\n    Copyright (c) Microsoft Corporation.\n\n"
    "    Permission is hereby granted, free of charge, to any person obtaining a copy\n"
    "    of this software and associated documentation files\n"
)


def test_offline_miss_is_an_error_not_a_request(tmp_path):
    with pytest.raises(ms.SourceUnavailable, match="offline"):
        ms.fetch("LICENSE", tmp_path, offline=True)


def test_host_outside_the_allow_list_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "RAW_BASE", "https://example.com/fluent")
    with pytest.raises(ms.SourceUnavailable, match="не из разрешённых"):
        ms.fetch("LICENSE", tmp_path)


def test_unexpected_license_stops_the_build(tmp_path):
    """Сначала право, потом файлы: под неизвестной лицензией набор не собирается."""
    seed_cache(tmp_path, "LICENSE", b"GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n")
    with pytest.raises(SystemExit, match="MIT"):
        ms.fetch_license(tmp_path, offline=True)


def test_mit_license_is_accepted(tmp_path):
    seed_cache(tmp_path, "LICENSE", MIT_TEXT.encode())
    assert "MIT License" in ms.fetch_license(tmp_path, offline=True)


def test_wrong_folder_is_caught_by_its_glyph(tmp_path):
    """Таблица папок набрана руками — опечатку в ней ловит сверка с emoji."""
    seed_cache(tmp_path, "assets/Bank/metadata.json", json.dumps({"glyph": "🏨"}).encode())
    with pytest.raises(ms.SourceUnavailable, match="FLUENT_DIRS"):
        ms.fetch_source("bank", "🏦", tmp_path, offline=True)


def test_a_page_instead_of_a_picture_is_not_a_sticker(tmp_path):
    seed_cache(tmp_path, "assets/Bank/metadata.json", json.dumps({"glyph": "🏦"}).encode())
    seed_cache(tmp_path, "assets/Bank/3D/bank_3d.png", b"<html>404</html>")
    with pytest.raises(ms.SourceUnavailable, match="не PNG"):
        ms.fetch_source("bank", "🏦", tmp_path, offline=True)


def test_build_from_cache_fills_the_index_and_leaves_gaps_null(tmp_path, monkeypatch, small):
    """Весь путь без сети: кеш → PNG → индекс. Чего нет в источнике — `null`."""
    out_dir = tmp_path / "stickers"
    out_dir.mkdir()
    index_path = out_dir / "index.json"
    index_path.write_text(
        ms.dump_index(
            {
                "version": 1,
                "note": "проба",
                "source": None,
                "license": None,
                "stickers": {
                    "house": {"emoji": "🏠", "file": None, "aspect": None},
                    "bank": {"emoji": "🏦", "file": None, "aspect": None},
                    "family": {"emoji": "👪", "file": None, "aspect": None},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ms, "STICKERS_DIR", out_dir)
    monkeypatch.setattr(ms, "INDEX_PATH", index_path)

    cache = tmp_path / "cache"
    seed_cache(cache, "LICENSE", MIT_TEXT.encode())
    seed_cache(cache, "assets/House/metadata.json", json.dumps({"glyph": "🏠"}).encode())
    seed_cache(cache, "assets/House/3D/house_3d.png", png_bytes(disk()))
    # Для `bank` в кеше ничего нет — как при оборванной сети.

    assert ms.main(["--offline", "--cache", str(cache), "--jobs", "1"]) == 0

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["stickers"]["house"] == {"emoji": "🏠", "file": "house.png", "aspect": 1.0}
    assert index["stickers"]["bank"] == {"emoji": "🏦", "file": None, "aspect": None}
    assert index["stickers"]["family"] == {"emoji": "👪", "file": None, "aspect": None}
    assert list(index["stickers"]) == ["house", "bank", "family"], "ключи не тронуты"
    assert "fluentui-emoji" in index["source"] and "MIT" in index["license"]
    assert "MIT License" in (out_dir / ms.LICENSE_NAME).read_text(encoding="utf-8")
    with Image.open(out_dir / "house.png") as im:
        assert im.mode == "RGBA" and im.size == (ms.SIZE, ms.SIZE)


def test_cache_inside_the_repository_is_refused():
    """Исходники Fluent в git не нужны — кеш обязан лежать снаружи."""
    with pytest.raises(SystemExit, match="внутри репозитория"):
        ms.main(["--offline", "--cache", str(ms.REPO_ROOT / "data" / "sticker-cache")])
