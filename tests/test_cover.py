"""Обложка для Reels: кадрирование, вёрстка надписи, стили, запись файла.

Сети нет, фотографий людей нет: кадры синтетические, видео генерирует ffmpeg.
Ради чего тесты написаны: надпись не должна вылезать за безопасную зону и
закрывать лицо ни при каком тексте — ни при пустом, ни при слове на полстроки, ни
при двухстах символах; а чтение и запись обязаны работать по пути с кириллицей
(`tmp_path` на машине разработки именно такой, и OpenCV по нему файлы не видит).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from src.pipeline import cover
from src.pipeline.cover import (
    MAX_TEXT,
    STYLES,
    CoverError,
    compose,
    make_cover,
    pick_frame,
    read_image,
    sample_background,
    style_keys,
)

W, H = 1080, 1920
TEXT = "ТОП 3 ОШИБКИ В МОНТАЖЕ"
KEYS = style_keys()
# Зона надписи с допуском в пиксель на округление.
SAFE_LEFT, SAFE_RIGHT = 0.07, 0.93
SAFE_TOP, SAFE_BOTTOM = 0.55, 0.88

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="нужен ffmpeg/ffprobe",
)


def synthetic(width: int = 720, height: int = 1280) -> np.ndarray:
    """BGR-кадр без лиц: плавный градиент с шумом, чтобы цветокору было что красить."""
    ys, xs = np.mgrid[0:height, 0:width]
    rng = np.random.default_rng(7)
    img = np.stack(
        [
            60 + 120 * xs / width,
            90 + 100 * ys / height,
            150 - 80 * xs / width + 40 * ys / height,
        ],
        axis=-1,
    )
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


@pytest.fixture(scope="module")
def image() -> np.ndarray:
    return synthetic()


def pixels(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.int16)


# ------------------------------------------------------------------------- стили


def test_styles_registry() -> None:
    assert len(STYLES) == 8
    assert KEYS == [s.key for s in STYLES]
    assert len(set(KEYS)) == 8
    for style in STYLES:
        assert re.fullmatch(r"[a-z][a-z0-9_-]*", style.key), style.key
        assert set(style.title) == {"ru", "en"}
        assert style.title["ru"].strip() and style.title["en"].strip()
    assert MAX_TEXT == 80


@pytest.mark.parametrize("key", KEYS)
def test_style_composes_and_keeps_face_zone(image: np.ndarray, key: str) -> None:
    """Каждый стиль даёт 1080×1920 RGB, а надпись меняет кадр только в нижней половине."""
    plain = compose(image, "", key)
    captioned = compose(image, TEXT, key)
    for img in (plain, captioned):
        assert img.size == (W, H)
        assert img.mode == "RGB"

    diff = np.abs(pixels(captioned) - pixels(plain)).max(axis=2)
    rows = np.flatnonzero(diff.max(axis=1))
    assert rows.size, "надпись не нарисовалась"
    # Выше половины высоты — кадр без надписи, пиксель в пиксель: там лицо.
    assert rows.min() >= H // 2, f"надпись залезла на строку {rows.min()}"
    # Сама надпись (а не мягкое затемнение под ней) — это сильное отличие от
    # кадра, и начинается оно не выше безопасной зоны. Низ зоны проверяется по
    # вёрстке: затемнение законно доходит до самого края кадра.
    strong = np.flatnonzero((diff > 96).max(axis=1))
    assert strong.size
    assert strong.min() >= SAFE_TOP * H - 1, f"надпись начинается на строке {strong.min()}"


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize(
    "text",
    [
        TEXT,
        "ШОК!",
        "мой день",
        "Как я набрал 100 000 подписчиков за 30 дней без рекламы и вложений",
        "Ш" * 60,  # одно слово на полстроки обязано ужаться, а не вылезти
        "слово " * 40,  # 240 символов: обрезается до MAX_TEXT
    ],
)
def test_caption_block_inside_safe_zone(key: str, text: str) -> None:
    block = cover._caption_block(text, key, W, H)
    assert block is not None
    assert 1 <= len(block.lines) <= 4
    assert sum(ln.key for ln in block.lines) == 1
    pad = cover._look(key).type.pad
    # Границы вместе с полями стиля: плашки и обводка тоже внутри зоны.
    assert block.x0 - pad[0] * block.size >= SAFE_LEFT * W - 1
    assert block.x1 + pad[2] * block.size <= SAFE_RIGHT * W + 1
    assert block.y0 - pad[1] * block.size >= SAFE_TOP * H - 1
    assert block.y1 + pad[3] * block.size <= SAFE_BOTTOM * H + 1
    assert sum(len(ln.text) for ln in block.lines) <= MAX_TEXT


def test_long_single_word_fits_frame_width() -> None:
    """Проверка по пикселям, а не по вёрстке: белые буквы не выходят за поля."""
    grey = np.full((640, 360, 3), 110, np.uint8)
    img = pixels(compose(grey, "Сверхдлинноеслововоднустрокубезпробелов", "gradient"))
    white = np.flatnonzero((img.min(axis=2) > 245).any(axis=0))
    assert white.size, "текст не нарисовался"
    assert white.min() >= SAFE_LEFT * W - 1
    assert white.max() <= SAFE_RIGHT * W + 1


def test_sample_text_is_set_large() -> None:
    """Короткая надпись набирается крупно и в несколько строк, а не одной мелкой."""
    for key in KEYS:
        block = cover._caption_block(TEXT, key, W, H)
        assert block is not None
        assert 2 <= len(block.lines) <= 4, key
        assert block.size >= 96, (key, block.size)


def test_preposition_does_not_hang() -> None:
    """«В» не остаётся в конце строки: предлог уходит к своему слову."""
    for key in KEYS:
        block = cover._caption_block(TEXT, key, W, H)
        assert block is not None
        for ln in block.lines[:-1]:
            assert ln.text.split()[-1].upper() != "В", (key, [x.text for x in block.lines])


def test_key_line_holds_longest_word() -> None:
    block = cover._caption_block("Мой первый невероятный день", "plate", W, H)
    assert block is not None
    assert "НЕВЕРОЯТНЫЙ" in block.key.text


# ---------------------------------------------------------------- текст и регистр


def test_empty_text_gives_plain_cover(image: np.ndarray) -> None:
    a = compose(image, "", "neon", width=540, height=960)
    b = compose(image, "  \n\t ", "neon", width=540, height=960)
    c = compose(image, "🔥🚀", "neon", width=540, height=960)  # после чистки пусто
    assert a.size == (540, 960)
    assert a.tobytes() == b.tobytes() == c.tobytes()


def test_long_text_is_truncated(image: np.ndarray) -> None:
    long = "очень длинная надпись " * 10
    assert len(long) > 200
    cut = " ".join(long.split())[:MAX_TEXT]
    a = compose(image, long, "plate", width=540, height=960)
    b = compose(image, cut, "plate", width=540, height=960)
    assert a.size == (540, 960)
    assert a.tobytes() == b.tobytes()


def test_unknown_style_falls_back_to_first(image: np.ndarray) -> None:
    a = compose(image, TEXT, "нет-такого-стиля", width=540, height=960)
    b = compose(image, TEXT, KEYS[0], width=540, height=960)
    assert a.tobytes() == b.tobytes()


def test_compose_is_deterministic(image: np.ndarray) -> None:
    for key in ("tape", "marker"):  # стили с поворотом слоя
        a = compose(image, TEXT, key, width=540, height=960)
        b = compose(image, TEXT, key, width=540, height=960)
        assert a.tobytes() == b.tobytes()


def test_styles_differ_from_each_other(image: np.ndarray) -> None:
    renders = {k: compose(image, TEXT, k, width=270, height=480).tobytes() for k in KEYS}
    assert len(set(renders.values())) == len(KEYS)


def test_emoji_and_unknown_glyphs_are_dropped() -> None:
    for style in STYLES:
        fonts = cover._look(style.key).type.fonts()
        assert cover._clean_text("ТОП 🚀 3 🔥 ошибки‍️ 👍🏽", fonts) == "ТОП 3 ошибки"
        assert cover._clean_text("  много\n\nпробелов\t тут ", fonts) == "много пробелов тут"
        assert cover._clean_text("Ёж, «да»! 100%?", fonts).startswith("Ёж,")


def test_glyph_check_agrees_with_font_cmap() -> None:
    """Проверка «есть ли глиф» через растеризатор совпадает с таблицей шрифта."""
    ttlib = pytest.importorskip("fontTools.ttLib")
    probe = "АяЁёЖщ09Qz!?%«»—…№₽€→♥😀🚀"
    names = {f for look in cover._LOOKS for f in look.type.fonts()}
    for name in sorted(names):
        cmap = ttlib.TTFont(str(cover.FONTS_DIR / f"{name}.ttf"))["cmap"].getBestCmap()
        for ch in probe:
            assert cover._has_glyph(name, ch) == (ord(ch) in cmap), (name, ch)


def test_compose_accepts_odd_inputs() -> None:
    grey = np.full((300, 400), 128, np.uint8)  # одноканальный альбомный
    assert compose(grey, TEXT, "paper", width=270, height=480).size == (270, 480)
    bgra = np.zeros((90, 60, 4), np.uint8)  # крошечный с альфой
    assert compose(bgra, TEXT, "duotone", width=270, height=480).size == (270, 480)
    with pytest.raises(CoverError):
        compose(np.zeros((0, 0, 3), np.uint8), TEXT, "plate")


# ------------------------------------------------------------------- кадрирование


def test_crop_box_without_face_is_centred() -> None:
    x, y, w, h = cover._crop_box(1920, 1080, None, W, H)
    assert h == 1080
    assert abs(w / h - 9 / 16) < 0.002
    assert abs((x + w / 2) - 960) <= 1 and y == 0

    x, y, w, h = cover._crop_box(1080, 2400, None, W, H)
    assert (w, h) == (1080, 1920)
    assert (x, y) == (0, 240)


def test_crop_box_puts_face_in_place() -> None:
    face = cover._Face(x=1300, y=430, w=200, h=260, score=0.9)
    x, y, w, h = cover._crop_box(1920, 1080, face, W, H)
    assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080
    assert abs(w / h - 9 / 16) < 0.003
    assert abs((face.x - x) / w - 0.5) < 0.01  # по горизонтали посередине
    assert abs((face.y - y) / h - 0.38) < 0.02  # центр лица на 38 % высоты
    assert 0.30 <= face.w / w <= 0.40


def test_crop_box_clamps_and_limits_upscale() -> None:
    # Лицо в углу: окно не выходит за кадр.
    face = cover._Face(x=40, y=40, w=60, h=80, score=0.9)
    x, y, w, h = cover._crop_box(1920, 1080, face, W, H)
    assert (x, y) == (0, 0)
    assert x + w <= 1920 and y + h <= 1080
    # Мелкое лицо на 4K: окно не уже половины ширины обложки (апскейл не больше 2×).
    face = cover._Face(x=1900, y=900, w=90, h=120, score=0.9)
    x, y, w, h = cover._crop_box(3840, 2160, face, W, H)
    assert w >= W / 2 - 1
    # Лицо во весь кадр: кроп не может ничего, и это не ошибка.
    face = cover._Face(x=360, y=700, w=620, h=780, score=0.9)
    assert cover._crop_box(720, 1280, face, W, H) == (0, 0, 720, 1280)


# ------------------------------------------------------------------- выбор кадра


def probe(t: float, **kw: float) -> cover._Probe:
    face = kw.pop("face", True)
    p = cover._Probe(t=t, path=Path(f"{t}.jpg"), sharp=kw.pop("sharp", 100.0))
    if face:
        p.face = cover._Face(x=360, y=500, w=200, h=260, score=0.9)
        p.share = kw.pop("share", 0.25)
        p.offset = kw.pop("offset", 0.0)
        p.frontal = kw.pop("frontal", 1.0)
        p.face_sharp = kw.pop("face_sharp", 100.0)
        p.eye = kw.pop("eye", 1.0)
    assert not kw
    return p


def test_best_probe_prefers_frontal_sharp_open_eyes() -> None:
    good = probe(5.0)
    turned = probe(4.0, frontal=0.2)
    blurry = probe(6.0, face_sharp=15.0)
    blink = probe(7.0, eye=0.4)
    aside = probe(3.0, offset=0.9)
    for other in (turned, blurry, blink, aside):
        assert cover._best_probe([other, good], 10.0) is good
        assert cover._best_probe([good, other], 10.0) is good
    # Любое лицо лучше кадра без лица, каким бы резким тот ни был.
    empty = probe(5.0, face=False, sharp=9999.0)
    assert cover._best_probe([empty, turned], 10.0) is turned


def test_best_probe_without_faces_takes_sharpest_of_middle_third() -> None:
    probes = [
        probe(1.0, face=False, sharp=900.0),  # самый резкий, но в начале
        probe(4.0, face=False, sharp=200.0),
        probe(5.0, face=False, sharp=300.0),
        probe(9.0, face=False, sharp=800.0),
    ]
    assert cover._best_probe(probes, 10.0) is probes[2]
    # В средней трети проб нет вовсе — берётся самый резкий из всех.
    edges = [probes[0], probes[3]]
    assert cover._best_probe(edges, 10.0) is probes[0]


def test_sample_times_skip_edges() -> None:
    times = cover._sample_times(100.0, 24)
    assert len(times) == 24
    assert times == sorted(times)
    assert times[0] >= 5.0 and times[-1] <= 95.0
    assert len(cover._sample_times(1.0, 24)) < 24  # короткий ролик — проб меньше
    assert cover._sample_times(None, 24)[0] == 0.0


def test_face_finder_never_raises(image: np.ndarray) -> None:
    finder = cover._face_finder()
    assert finder.name in {"yunet", "haar", "none"}
    assert finder.find(image) == []
    assert finder.find(np.zeros((3, 3, 3), np.uint8)) == []


def test_haar_fallback_loads_from_memory() -> None:
    """Запасной каскад грузится мимо пути (на Windows путь к нему с кириллицей)."""
    cascade = cover._FaceFinder._make_haar()
    assert cascade is not None and not cascade.empty()


# --------------------------------------------------------------------------- файлы


def test_read_image_jpeg(tmp_path: Path) -> None:
    path = tmp_path / "фото пользователя.jpg"
    Image.new("RGB", (64, 48), (200, 30, 40)).save(path, quality=95)
    img = read_image(path)
    assert img.shape == (48, 64, 3) and img.dtype == np.uint8
    b, g, r = (int(v) for v in img[24, 32])
    assert abs(r - 200) < 6 and abs(g - 30) < 6 and abs(b - 40) < 6  # порядок BGR


def test_read_image_applies_exif_rotation(tmp_path: Path) -> None:
    path = tmp_path / "повёрнутое.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # телефон держали вертикально: кадр лежит на боку
    Image.new("RGB", (64, 32), (10, 120, 220)).save(path, exif=exif)
    assert read_image(path).shape == (64, 32, 3)


def test_read_image_png_with_alpha(tmp_path: Path) -> None:
    path = tmp_path / "прозрачное.png"
    Image.new("RGBA", (20, 30), (0, 0, 0, 0)).save(path)
    img = read_image(path)
    assert img.shape == (30, 20, 3)
    assert int(img.min()) == 255  # прозрачное легло на белое


def test_read_image_errors(tmp_path: Path) -> None:
    with pytest.raises(CoverError):
        read_image(tmp_path / "нет такого.jpg")
    junk = tmp_path / "мусор.jpg"
    junk.write_bytes(b"this is not an image")
    with pytest.raises(CoverError):
        read_image(junk)


def test_make_cover_from_photo(tmp_path: Path) -> None:
    src = tmp_path / "исходник.jpg"
    Image.fromarray(synthetic(600, 800)[:, :, ::-1]).save(src, quality=92)
    out = tmp_path / "обложки" / "cover.jpg"
    make_cover(src, out, text=TEXT, style="tape", from_video=False)
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (W, H)
        im.load()
    assert not list(out.parent.glob("*.part"))


@needs_ffmpeg
def test_make_cover_from_video(tmp_path: Path) -> None:
    video = tmp_path / "ролик.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=2",
            "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )  # fmt: skip
    out = tmp_path / "cover.jpg"
    make_cover(video, out, text=TEXT, style="outline", from_video=True)
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (W, H)
        im.load()
        # Кадр настоящий, а не чёрный: у testsrc2 яркая цветная картинка.
        assert np.asarray(im.convert("L")).std() > 20


@needs_ffmpeg
def test_pick_frame_errors(tmp_path: Path) -> None:
    with pytest.raises(CoverError):
        pick_frame(tmp_path / "нет такого.mp4")
    junk = tmp_path / "мусор.mp4"
    junk.write_bytes(b"\x00" * 2048)
    with pytest.raises(CoverError):
        pick_frame(junk)


# ----------------------------------------------------------------- фон для превью


def test_sample_background() -> None:
    bg = sample_background(270, 480)
    assert bg.shape == (480, 270, 3) and bg.dtype == np.uint8
    assert np.array_equal(bg, sample_background(270, 480))
    assert bg.std() > 10  # не заливка
    # Нарисованный силуэт — не лицо: детектор его не находит, кадр идёт по центру.
    assert cover._main_face(sample_background(540, 960)) is None
