"""Обложка для Reels: кадр с лицом → 9:16 → надпись в одном из восьми стилей.

Без платных API и без сети: всё рисуется Pillow шрифтами из `assets/fonts`,
лицо ищет тот же YuNet, что и автокроп (`src/pipeline/crop.py`).

Схема: `pick_frame` (или `read_image` для фото пользователя) даёт BGR-картинку →
`compose` кадрирует её в 9:16 вокруг лица, красит по стилю и рисует надпись →
`make_cover` пишет JPEG 1080×1920.

Про зону надписи. Надпись живёт в полосе 56–87 % высоты: выше лицо (его центр
кадрирование ставит на 38 %), ниже 88 % — интерфейс Instagram, а сетка профиля
вдобавок режет обложку до центральных 3:4. Чтобы «надпись не лезет на лицо»
было свойством кода, а не удачей подбора отступов, слой надписи в конце
смешивается с чистым кадром по маске: выше 50 % высоты всегда остаётся кадр без
надписи, пиксель в пиксель (на этом стоит тест).

Про пути. На машине разработки репозиторий лежит в папке с кириллицей, и OpenCV
не открывает по таким путям ни картинки, ни модель YuNet, ни каскад Хаара.
Поэтому `cv2.imread`/`cv2.imwrite`/`cv2.VideoCapture` здесь нет вовсе: файлы
читает Python, а OpenCV и Pillow получают байты. Детектор собирается по цепочке
«YuNet по пути → YuNet из байтов → Хаар из памяти → без детектора»; последний
шаг значит «лица нет, кадрируем по центру», а не падение.

Про одинаковость. Вёрстка обязана быть детерминированной: один и тот же вход
даёт одну и ту же картинку. Отсюда принудительный `Layout.BASIC` у шрифтов —
иначе там, где у Pillow есть libraqm (Linux), кернинг и ширины строк были бы
другими, чем там, где его нет (Windows), и строка, влезшая у разработчика,
могла бы не влезть на проде.
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Iterator

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FONTS_DIR = REPO_ROOT / "assets" / "fonts"
YUNET_MODEL = REPO_ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"

MAX_TEXT = 80

# Кадры для оценки: длинная сторона не больше этого. Детектору и резкости хватает,
# а 24 кадра 4K в памяти были бы под гигабайт.
_SCORE_SIDE = 1280
# Растр детектора. YuNet уверенно берёт лица от ~20 px, а время линейно по площади.
_DETECT_SIDE = 960
# Потолок для кадра, из которого собирается обложка. 4K-горизонталь нужна целиком:
# из неё вырезается узкое вертикальное окно вокруг лица.
_FULL_SIDE = 3840
_FFMPEG_TIMEOUT = 60.0  # на один кадр
# Сколько кадров достаёт один запуск ffmpeg. Больше — меньше запусков, но столько
# же декодеров живёт одновременно, а на 4K это сотня мегабайт на каждый.
_BATCH = 4
_FFPROBE_TIMEOUT = 30.0

# Кадрирование: ширина бокса лица в долях ширины обложки, высота центра лица от
# верха и предел апскейла исходника.
_FACE_WIDTH = 0.34
_FACE_WIDTH_MAX = 0.40
_FACE_Y = 0.38
_MAX_UPSCALE = 2.0

# Выше этой доли высоты надпись не появляется никогда (см. докстринг модуля).
_FACE_ZONE = 0.50
_FACE_ZONE_FADE = 0.035

# Зона надписи по умолчанию: слева, сверху, справа, снизу — в долях кадра.
_BOX = (0.07, 0.56, 0.93, 0.87)


class CoverError(RuntimeError):
    """Понятная причина, по которой обложку не собрать: нет кадров, файл не читается."""


@dataclass(frozen=True)
class CoverStyle:
    key: str  # латиница, стабильный идентификатор
    title: dict[str, str]  # {"ru": "...", "en": "..."} — короткое название стиля


# --------------------------------------------------------------------------- лица


@dataclass(frozen=True, slots=True)
class _Face:
    x: float  # центр бокса, пиксели кадра
    y: float
    w: float
    h: float
    score: float
    # Ландмарки YuNet: правый глаз, левый глаз, нос. У Хаара их нет.
    eyes: tuple[tuple[float, float], tuple[float, float]] | None = None
    nose: tuple[float, float] | None = None


class _FaceFinder:
    """Детектор лиц, который не умеет падать: в худшем случае лиц просто нет."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # cv2-детекторы не потокобезопасны
        self._yunet = self._make_yunet()
        self._haar = None if self._yunet is not None else self._make_haar()
        if self._yunet is not None:
            self.name = "yunet"
        elif self._haar is not None:
            self.name = "haar"
        else:
            self.name = "none"
            log.warning("детектор лиц недоступен: обложка кадрируется по центру")

    @staticmethod
    def _make_yunet():  # noqa: ANN205 — cv2.FaceDetectorYN без публичного типа
        # 1) Детектор проекта: на проде путь к модели латинский, и это тот же
        #    объект, которым кадрирует автокроп. Сырой cv2-детектор нужен ради
        #    ландмарок глаз и носа: в `Detection` их координат по X нет.
        try:
            from src.pipeline.crop import YuNetDetector

            net = getattr(YuNetDetector(), "_net", None)
            if net is not None:
                return net
        except Exception as e:  # noqa: BLE001 — детектор не должен ронять обложку
            log.debug("YuNet по пути не собрался (%s), пробую из байтов", e)
        # 2) Та же модель, но байтами: OpenCV не открывает путь с кириллицей.
        try:
            model = np.fromfile(str(YUNET_MODEL), dtype=np.uint8)
            return cv2.FaceDetectorYN.create(
                "onnx", model, np.empty(0, np.uint8), (320, 320), 0.6, 0.3, 5000
            )
        except Exception as e:  # noqa: BLE001
            log.warning("YuNet недоступен (%s), пробую каскад Хаара", e)
            return None

    @staticmethod
    def _make_haar():  # noqa: ANN205
        try:
            path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            xml = path.read_text(encoding="utf-8")
            storage = cv2.FileStorage(xml, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
            try:
                cascade = cv2.CascadeClassifier()
                ok = cascade.read(storage.getFirstTopLevelNode())
            finally:
                storage.release()
            if not ok or cascade.empty():
                raise RuntimeError("каскад пуст")
            return cascade
        except Exception as e:  # noqa: BLE001
            log.warning("каскад Хаара недоступен (%s)", e)
            return None

    def find(self, frame: np.ndarray) -> list[_Face]:
        """Лица на BGR-кадре, координаты — в пикселях этого кадра."""
        if self._yunet is None and self._haar is None:
            return []
        h, w = frame.shape[:2]
        k = min(1.0, _DETECT_SIDE / max(h, w))
        small = frame
        if k < 1.0:
            small = cv2.resize(
                frame, (max(2, round(w * k)), max(2, round(h * k))), interpolation=cv2.INTER_AREA
            )
        try:
            with self._lock:
                find = self._find_yunet if self._yunet is not None else self._find_haar
                faces = find(small)
        except Exception as e:  # noqa: BLE001 — кривой кадр не повод остаться без обложки
            log.warning("детектор лиц упал на кадре (%s)", e)
            return []
        return [_rescale_face(f, 1.0 / k) for f in faces] if k < 1.0 else faces

    def _find_yunet(self, frame: np.ndarray) -> list[_Face]:
        h, w = frame.shape[:2]
        self._yunet.setInputSize((w, h))
        _, rows = self._yunet.detect(frame)
        out: list[_Face] = []
        for r in rows if rows is not None else ():
            fx, fy, fw, fh = (float(v) for v in r[:4])
            if fw <= 1 or fh <= 1:
                continue
            marks = len(r) >= 15
            out.append(
                _Face(
                    x=fx + fw / 2,
                    y=fy + fh / 2,
                    w=fw,
                    h=fh,
                    score=float(r[-1]),
                    # r[4:6] — правый глаз, r[6:8] — левый, r[8:10] — нос.
                    eyes=((float(r[4]), float(r[5])), (float(r[6]), float(r[7])))
                    if marks
                    else None,
                    nose=(float(r[8]), float(r[9])) if marks else None,
                )
            )
        return out

    def _find_haar(self, frame: np.ndarray) -> list[_Face]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        boxes = self._haar.detectMultiScale(
            gray, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48)
        )
        return [
            _Face(x=float(x) + w / 2, y=float(y) + h / 2, w=float(w), h=float(h), score=0.5)
            for (x, y, w, h) in boxes
        ]


def _rescale_face(f: _Face, k: float) -> _Face:
    eyes = None
    if f.eyes is not None:
        (rx, ry), (lx, ly) = f.eyes
        eyes = ((rx * k, ry * k), (lx * k, ly * k))
    return _Face(
        x=f.x * k,
        y=f.y * k,
        w=f.w * k,
        h=f.h * k,
        score=f.score,
        eyes=eyes,
        nose=None if f.nose is None else (f.nose[0] * k, f.nose[1] * k),
    )


_finder_lock = threading.Lock()
_finder: _FaceFinder | None = None


def _face_finder() -> _FaceFinder:
    global _finder
    with _finder_lock:
        if _finder is None:
            _finder = _FaceFinder()
        return _finder


def _main_face(frame: np.ndarray) -> _Face | None:
    """Главное лицо кадра: крупное, уверенное и не у самого края."""
    faces = _face_finder().find(frame)
    if not faces:
        return None
    w = frame.shape[1]

    def weight(f: _Face) -> float:
        off = min(1.0, abs(f.x / w - 0.5) * 2)
        return f.w * f.h * (0.5 + 0.5 * f.score) * (1.0 - 0.4 * off)

    return max(faces, key=weight)


# --------------------------------------------------------------------- выбор кадра


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Внешний процесс без shell. None — не уложился в срок или завершился с ошибкой."""
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise CoverError(f"не найден {cmd[0]}: без него кадр из видео не достать") from e
    except subprocess.TimeoutExpired:
        log.warning("%s не уложился в %.0f с", cmd[0], timeout)
        return None
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        log.debug("%s вернул %d: %s", cmd[0], p.returncode, " | ".join(tail))
        return None
    return p


def _duration(video: Path) -> float | None:
    """Длительность ролика в секундах. None — контейнер её не знает.

    Файл, который ffprobe не открыл вовсе, не откроет и ffmpeg: это сразу
    `CoverError`, а не два десятка обречённых попыток достать кадр.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, timeout=_FFPROBE_TIMEOUT)
    except FileNotFoundError as e:
        raise CoverError("не найден ffprobe: без него кадр из видео не достать") from e
    except subprocess.TimeoutExpired:
        log.warning("ffprobe не уложился в %.0f с", _FFPROBE_TIMEOUT)
        return None
    if p.returncode != 0:
        raise CoverError(f"видео не читается: {video.name}")
    try:
        value = float(p.stdout.decode("utf-8", "replace").strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _sample_times(duration: float | None, samples: int) -> list[float]:
    """Моменты проб: равномерно, без первых и последних 5 % ролика."""
    samples = max(1, int(samples))
    if duration is None:
        # Длительность не узнали (битый контейнер, поток): щупаем начало посекундно.
        return [float(i) for i in range(min(samples, 8))]
    start, end = 0.05 * duration, 0.95 * duration
    # Чаще четырёх проб в секунду смысла нет: соседние кадры почти одинаковы.
    n = max(1, min(samples, int((end - start) * 4) + 1))
    return [start + (end - start) * (i + 0.5) / n for i in range(n)]


def _read_bgr(path: Path) -> np.ndarray | None:
    """Картинка с диска мимо `cv2.imread` — тот не открывает пути с кириллицей."""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img if img is not None and img.size else None


def _grab_many(video: Path, shots: list[tuple[float, Path]], max_side: int, quality: int) -> bool:
    """Кадры в заданные моменты одним запуском ffmpeg: по входу с `-ss` на кадр.

    Один процесс на несколько кадров, потому что на запуск ffmpeg уходит больше,
    чем на сам кадр (на Windows — секунда на процесс). Перемотка стоит ДО `-i`:
    декодируется только хвост от ближайшего ключевого кадра, а не весь ролик.
    """
    scale = (
        f"scale=w='if(gt(iw,ih),min({max_side},iw),-2)'"
        f":h='if(gt(iw,ih),-2,min({max_side},ih))':flags=lanczos"
    )
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for t, _ in shots:
        # Два потока на декодер: кадр нужен один, а память на 4K растёт с потоками.
        cmd += ["-threads", "2", "-ss", f"{max(0.0, t):.3f}", "-i", str(video)]
    for i, (_, out) in enumerate(shots):
        cmd += ["-map", f"{i}:v:0", "-frames:v", "1", "-vf", scale]
        cmd += ["-q:v", str(quality), "-update", "1", str(out)]
    return _run(cmd, _FFMPEG_TIMEOUT * len(shots)) is not None


def _grab(video: Path, t: float, out: Path, max_side: int, quality: int) -> np.ndarray | None:
    """Один кадр в момент `t`, длинная сторона не больше `max_side`."""
    _grab_many(video, [(t, out)], max_side, quality)
    return _read_bgr(out)


def _grab_samples(
    video: Path, times: list[float], folder: Path
) -> Iterator[tuple[float, Path, np.ndarray]]:
    """Пробы пачками по `_BATCH`; что не получилось пачкой, добирается по одному.

    Генератор: кадр отдаётся, обмеряется и забывается, в памяти их не копится.
    """
    shots = [(t, folder / f"s{i:03d}.jpg") for i, t in enumerate(times)]
    for start in range(0, len(shots), _BATCH):
        batch = shots[start : start + _BATCH]
        ok = _grab_many(video, batch, _SCORE_SIDE, quality=3)
        for t, path in batch:
            frame = _read_bgr(path)
            if frame is None and not ok and len(batch) > 1:
                # Один момент за концом ролика мог уронить всю пачку.
                frame = _grab(video, t, path, _SCORE_SIDE, quality=3)
            if frame is not None:
                yield t, path, frame


@dataclass(slots=True)
class _Probe:
    """Замер одного кадра-кандидата. Сам кадр не держим — только числа."""

    t: float
    path: Path
    sharp: float  # резкость всего кадра
    face: _Face | None = None
    share: float = 0.0  # высота лица в долях высоты кадра
    offset: float = 0.0  # 0 — лицо по центру по горизонтали, 1 — у края
    frontal: float = 0.0  # 1 — анфас
    face_sharp: float = 0.0
    eye: float | None = None  # контраст в области глаз; ниже обычного — моргнул


def _sharpness(gray: np.ndarray, side: int) -> float:
    """Дисперсия лапласиана на приведённом растре: чем резче, тем больше."""
    h, w = gray.shape[:2]
    if h < 2 or w < 2:
        return 0.0
    k = side / max(h, w)
    size = (max(2, round(w * k)), max(2, round(h * k)))
    small = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(small, cv2.CV_64F).var())


def _measure(t: float, path: Path, frame: np.ndarray) -> _Probe:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    probe = _Probe(t=t, path=path, sharp=_sharpness(gray, 640))
    face = _main_face(frame)
    if face is None:
        return probe
    probe.face = face
    probe.share = face.h / h
    probe.offset = min(1.0, abs(face.x / w - 0.5) * 2)

    x0, x1 = int(max(0, face.x - face.w / 2)), int(min(w, face.x + face.w / 2))
    y0, y1 = int(max(0, face.y - face.h / 2)), int(min(h, face.y + face.h / 2))
    roi = gray[y0:y1, x0:x1]
    # Резкость меряем по лицу: размытый фон у портрета — достоинство, а не брак.
    probe.face_sharp = _sharpness(roi, 128) if roi.size else 0.0

    if face.eyes is None or face.nose is None:
        probe.frontal = 0.6  # фронтальный каскад Хаара профиль всё равно не находит
        return probe
    (rx, ry), (lx, ly) = face.eyes
    dist = math.hypot(lx - rx, ly - ry)
    if dist < 2:
        probe.frontal = 0.3
        return probe
    # Поворот головы: на анфасе нос стоит посередине между глазами, на повороте
    # уезжает к одному из них. Наклон головы штрафуем слабее — он заметен меньше.
    yaw = abs(face.nose[0] - (rx + lx) / 2) / dist
    roll = abs(math.degrees(math.atan2(ly - ry, lx - rx)))
    roll = min(roll, abs(180 - roll))
    probe.frontal = max(0.0, 1.0 - yaw / 0.45) * (1.0 - 0.4 * min(1.0, roll / 30.0))

    # Открытые глаза — это тёмный зрачок на светлом белке, то есть высокий
    # контраст пятна вокруг ландмарки; у закрытого века он заметно ниже. Число
    # сравнивается с медианой по ролику, а не с порогом: у очков и у тёмных глаз
    # свой «обычный» уровень.
    r = max(3, int(0.22 * dist))
    spread = float(roi.std()) if roi.size else 0.0
    values = []
    for ex, ey in face.eyes:
        patch = gray[max(0, int(ey) - r) : int(ey) + r, max(0, int(ex) - r) : int(ex) + r]
        if patch.size:
            values.append(float(patch.std()))
    if values and spread > 1e-3:
        probe.eye = sum(values) / len(values) / spread
    return probe


def _best_probe(probes: list[_Probe], duration: float | None) -> _Probe:
    middle = (duration or (probes[-1].t + probes[0].t)) / 2
    faces = [p for p in probes if p.face is not None]
    if not faces:
        # Лица нет нигде: самый резкий кадр из средней трети ролика.
        lo, hi = probes[0].t, probes[-1].t
        if duration:
            lo, hi = duration / 3, 2 * duration / 3
        else:
            lo, hi = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        pool = [p for p in probes if lo <= p.t <= hi] or probes
        return max(pool, key=lambda p: (p.sharp, -abs(p.t - middle)))

    top_sharp = max(p.face_sharp for p in faces) or 1.0
    eyes = [p.eye for p in faces if p.eye is not None]
    usual_eye = median(eyes) if eyes else None

    def score(p: _Probe) -> float:
        assert p.face is not None
        sharp = p.face_sharp / top_sharp
        # Крупнее — лучше, но только до четверти высоты: дальше обложке всё равно,
        # а лицо во весь кадр кадрировать уже некуда.
        size = min(1.0, p.share / 0.25) - max(0.0, p.share - 0.6)
        total = (
            1.0 * size
            + 0.6 * (1.0 - p.offset)
            + 1.2 * p.frontal
            + 1.0 * sharp
            + 0.4 * p.face.score
        )
        total -= 0.6 * max(0.0, (0.4 - sharp) / 0.4)  # смаз
        if usual_eye and p.eye is not None:
            total -= 0.8 * min(1.0, max(0.0, (0.75 - p.eye / usual_eye) / 0.75) * 3)  # моргнул
        return total

    return max(faces, key=lambda p: (round(score(p), 6), -abs(p.t - middle)))


def pick_frame(video: Path, *, samples: int = 24) -> np.ndarray:
    """BGR-кадр ролика, на котором лицо видно лучше всего.

    Пробы берутся уменьшенными (быстро и мало памяти), а победивший момент
    достаётся из видео ещё раз в полном разрешении — обложка обязана быть резкой.
    """
    video = Path(video)
    if not video.is_file():
        raise CoverError(f"видео не найдено: {video.name}")
    duration = _duration(video)
    tmp = Path(tempfile.mkdtemp(prefix="cover-"))
    try:
        probes: list[_Probe] = []
        for t, path, frame in _grab_samples(video, _sample_times(duration, samples), tmp):
            probes.append(_measure(t, path, frame))
        if not probes:
            # Ролик короче шага проб или длительность в контейнере врёт: первый кадр.
            path = tmp / "first.jpg"
            frame = _grab(video, 0.0, path, _SCORE_SIDE, quality=3)
            if frame is not None:
                probes.append(_measure(0.0, path, frame))
        if not probes:
            raise CoverError("из видео не удалось достать ни одного кадра")
        best = _best_probe(probes, duration)
        log.info(
            "обложка: кадр %.2f с из %d проб, лицо %s",
            best.t,
            len(probes),
            "есть" if best.face is not None else "не найдено",
        )
        full = _grab(video, best.t, tmp / "full.jpg", _FULL_SIDE, quality=1)
        if full is None:
            full = _read_bgr(best.path)
        if full is None:
            raise CoverError("из видео не удалось достать ни одного кадра")
        return full
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_image(path: Path) -> np.ndarray:
    """Фото пользователя в BGR с учётом EXIF-поворота (телефон пишет его в метку)."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise CoverError(f"файл не читается: {path.name}") from e
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (_FULL_SIDE, _FULL_SIDE))  # JPEG сразу декодируется меньше
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                # Прозрачность кладём на белое: «чёрные дыры» вместо фона хуже.
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
            im = im.convert("RGB")
            if max(im.size) > _FULL_SIDE:
                im.thumbnail((_FULL_SIDE, _FULL_SIDE), Image.Resampling.LANCZOS)
            rgb = np.asarray(im, dtype=np.uint8)
    except Exception as e:  # noqa: BLE001 — Pillow кидает что угодно, от OSError до бомбы
        raise CoverError(f"не удалось прочитать изображение: {path.name}") from e
    if rgb.ndim != 3 or rgb.shape[0] < 2 or rgb.shape[1] < 2:
        raise CoverError(f"не удалось прочитать изображение: {path.name}")
    return np.ascontiguousarray(rgb[:, :, ::-1])


# ------------------------------------------------------------------- кадрирование


def _as_bgr(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.size == 0 or image.ndim not in (2, 3):
        raise CoverError("пустое изображение")
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    if image.shape[2] == 1:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] != 3:
        raise CoverError("непонятный формат изображения")
    return image


def _crop_box(
    iw: int, ih: int, face: _Face | None, width: int, height: int
) -> tuple[int, int, int, int]:
    """Окно 9:16 в исходнике: x, y, ширина, высота.

    С лицом: бокс лица занимает `_FACE_WIDTH` ширины обложки, центр лица стоит по
    горизонтали посередине и на `_FACE_Y` высоты. Окно не выходит за кадр и не
    бывает меньше, чем позволяет предел апскейла.

    Если лицо в исходнике стоит так, что окно нужного размера упирается в край
    кадра (типично для горизонтального видео: по высоте запаса почти нет), окно
    сужается — до `_FACE_WIDTH_MAX` ширины под лицо. Место лица важнее его
    точного размера: надпись рассчитана на лицо в верхней части обложки.
    """
    ar = width / height
    widest = min(float(iw), ih * ar)
    if face is None:
        cw = widest
        ch = cw / ar
        x, y = (iw - cw) / 2, (ih - ch) / 2
    else:
        # Самое большое окно, в котором лицо ещё встаёт на своё место, не упираясь
        # в края кадра: сверху, снизу и по бокам.
        fits = min(
            face.y / _FACE_Y * ar,
            (ih - face.y) / (1.0 - _FACE_Y) * ar,
            2 * face.x,
            2 * (iw - face.x),
        )
        floor = max(face.w / _FACE_WIDTH_MAX, width / _MAX_UPSCALE)
        cw = max(min(face.w / _FACE_WIDTH, fits), floor)
        cw = min(cw, widest)
        ch = cw / ar
        x, y = face.x - cw / 2, face.y - _FACE_Y * ch
    cw_i = max(1, min(iw, round(cw)))
    ch_i = max(1, min(ih, round(ch)))
    x_i = int(min(max(0, round(x)), iw - cw_i))
    y_i = int(min(max(0, round(y)), ih - ch_i))
    return x_i, y_i, cw_i, ch_i


def _frame_photo(image: np.ndarray, width: int, height: int) -> Image.Image:
    bgr = _as_bgr(image)
    ih, iw = bgr.shape[:2]
    x, y, cw, ch = _crop_box(iw, ih, _main_face(bgr), width, height)
    part = bgr[y : y + ch, x : x + cw]
    interp = cv2.INTER_AREA if cw >= width else cv2.INTER_LANCZOS4
    part = cv2.resize(part, (width, height), interpolation=interp)
    return Image.fromarray(np.ascontiguousarray(part[:, :, ::-1]))


# --------------------------------------------------------------------------- шрифты

_REF = 100  # кегль, на котором обмеряются строки при подборе размера
# Шрифты кэшируются на процесс, а объект FreeType не потокобезопасен: обмер и
# рисование текста идут по одному, даже если обложки собирают из разных потоков.
_type_lock = threading.Lock()


@lru_cache(maxsize=32)
def _font_bytes(name: str) -> bytes:
    try:
        return (FONTS_DIR / f"{name}.ttf").read_bytes()
    except OSError as e:
        raise CoverError(f"нет шрифта {name}.ttf в assets/fonts") from e


@lru_cache(maxsize=256)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    # Из байтов: FreeType на Windows не открывает путь с кириллицей. BASIC — см.
    # докстринг модуля про одинаковую вёрстку.
    return ImageFont.truetype(
        io.BytesIO(_font_bytes(name)), max(1, size), layout_engine=ImageFont.Layout.BASIC
    )


@lru_cache(maxsize=4096)
def _has_glyph(name: str, ch: str) -> bool:
    """Умеет ли шрифт рисовать символ.

    Через сам растеризатор, без fontTools (на проде его нет): отсутствующий символ
    FreeType рисует глифом `.notdef`, тем же, что и заведомо несуществующий U+FFFF.
    Пустая картинка — тоже «нет»: это невидимые соединители и селекторы эмодзи.
    """
    if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Mn", "Me"):
        return False
    font = _font(name, 48)
    try:
        mask = font.getmask(ch)
        if mask.getbbox() is None:
            return False
        blank = font.getmask("￿")
        return mask.size != blank.size or bytes(mask) != bytes(blank)
    except Exception:  # noqa: BLE001 — сомнительный символ проще выкинуть
        return False


# Чего в шрифте нет, но что можно честно заменить, а не выбросить.
_SUBSTITUTES = {
    "—": "-",
    "–": "-",
    "−": "-",
    "…": "...",
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "„": '"',
    "‘": "'",
    "’": "'",
    "№": "N",
    "₽": "р.",
}


def _clean_text(text: str, fonts: tuple[str, ...]) -> str:
    """Надпись, готовая к вёрстке: одна строка, не длиннее MAX_TEXT, без «тофу»."""
    text = unicodedata.normalize("NFC", text or "")
    text = " ".join(text.split())[:MAX_TEXT].strip()

    def ok(piece: str) -> bool:
        # Стиль может поднять или опустить регистр, поэтому проверяются все формы.
        forms = set(piece) | set(piece.upper()) | set(piece.lower())
        return all(_has_glyph(f, c) for f in fonts for c in forms if c != " ")

    out: list[str] = []
    for ch in text:
        if ch == " " or ok(ch):
            out.append(ch)
        elif ch in _SUBSTITUTES and ok(_SUBSTITUTES[ch]):
            out.append(_SUBSTITUTES[ch])
    return " ".join("".join(out).split())


# --------------------------------------------------------------------------- вёрстка


@dataclass(frozen=True)
class _Type:
    """Типографика стиля. Размеры — в долях кегля, кегль — при ширине кадра 1080."""

    font: str
    key_font: str = ""  # шрифт ключевой строки; пусто — тот же
    key_scale: float = 1.0  # во сколько раз ключевая строка крупнее остальных
    upper: bool = True
    key_case: str = "same"  # "same" — как остальные строки, "lower" — строчными
    # Шрифт ключевой строки только для строк без цифр: у курсива Playfair цифры
    # минускульные («3» проваливается под строку) и в наборе прописными выглядят
    # опечаткой, а переключить их на маюскульные без libraqm нечем.
    key_font_plain_digits: bool = False
    max_size: float = 150.0
    max_lines: int = 4
    gap: float = 0.36  # от базовой линии строки до верха прописных следующей
    key_drop: float = 0.0  # добавка к зазору после ключевой строки
    align: str = "center"  # "center" | "left"
    valign: str = "center"  # "center" | "bottom"
    pad: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # слева, сверху, справа, снизу
    box: tuple[float, float, float, float] = _BOX

    def fonts(self) -> tuple[str, ...]:
        return (self.font, self.key_font) if self.key_font else (self.font,)


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    font: ImageFont.FreeTypeFont
    size: int  # кегль этой строки, px
    x: float  # левый край чернил
    base: float  # базовая линия
    w: float  # ширина чернил
    cap: float  # высота прописных
    bearing: float  # отступ чернил от точки рисования
    key: bool


@dataclass(frozen=True, slots=True)
class _Block:
    lines: tuple[_Line, ...]
    size: int  # основной кегль, px
    x0: float  # границы чернил всех строк
    y0: float
    x1: float
    y1: float
    zone: tuple[float, float, float, float]  # отведённая зона, px

    @property
    def key(self) -> _Line:
        return next(ln for ln in self.lines if ln.key)


def _units(words: list[str]) -> list[str]:
    """Слова, сцепленные так, чтобы предлог не повис в конце строки.

    «В», «НА», «И», «НЕ» остаются со следующим словом — висячий предлог в крупной
    надписи читается как ошибка набора.
    """
    out: list[str] = []
    glue = ""
    for i, word in enumerate(words):
        last = i == len(words) - 1
        if not last and len(word) <= 2 and word.isalpha():
            glue += word + " "
            continue
        out.append(glue + word)
        glue = ""
    return out


def _key_unit(units: list[str]) -> int:
    """Ключевой кусок — тот, где самое длинное слово; при равенстве — последний."""
    best, best_len = 0, -1
    for i, unit in enumerate(units):
        longest = max(sum(c.isalnum() for c in w) for w in unit.split())
        if longest >= best_len:
            best, best_len = i, longest
    return best


def _cased(text: str, key: bool, typ: _Type) -> str:
    if key and typ.key_case == "lower":
        return text.lower()
    return text.upper() if typ.upper else text


def _line_font(typ: _Type, key: bool, text: str) -> tuple[str, float]:
    if not key:
        return typ.font, 1.0
    if typ.key_font_plain_digits and any(c.isdigit() for c in text):
        return typ.font, typ.key_scale
    return typ.key_font or typ.font, typ.key_scale


@lru_cache(maxsize=8192)
def _ink(name: str, size: int, text: str) -> tuple[float, float, float, float]:
    """Границы чернил строки относительно точки рисования на базовой линии."""
    return _font(name, size).getbbox(text, anchor="ls")


@lru_cache(maxsize=256)
def _cap_ratio(name: str) -> float:
    return -_ink(name, _REF, "Н")[1] / _REF


def _tail(text: str) -> float:
    """Запас под выносные элементы ниже базовой линии, в долях кегля строки."""
    return 0.26 if any(c.islower() for c in text) else 0.07


def _split(units: list[str], k: int, key_unit: int, typ: _Type) -> tuple[list[str], int, float]:
    """Лучшая разбивка на k строк: самая широкая строка как можно уже, строки ровные.

    Возвращает строки, номер ключевой и ширину самой широкой в долях кегля.
    """
    n = len(units)
    cache: dict[tuple[int, int], float] = {}

    def width(a: int, b: int) -> float:
        if (a, b) not in cache:
            key = a <= key_unit < b
            raw = " ".join(units[a:b])
            name, scale = _line_font(typ, key, raw)
            box = _ink(name, _REF, _cased(raw, key, typ))
            cache[(a, b)] = (box[2] - box[0]) * scale / _REF
        return cache[(a, b)]

    best: tuple[tuple[float, float], tuple[int, ...]] | None = None
    for cuts in combinations(range(1, n), k - 1):
        edges = (0, *cuts, n)
        widths = [width(a, b) for a, b in zip(edges, edges[1:])]
        rank = (round(max(widths), 4), round(max(widths) - min(widths), 4))
        if best is None or rank < best[0]:
            best = (rank, edges)
    assert best is not None
    edges = best[1]
    lines = [" ".join(units[a:b]) for a, b in zip(edges, edges[1:])]
    key = next(i for i, (a, b) in enumerate(zip(edges, edges[1:])) if a <= key_unit < b)
    return lines, key, best[0][0]


def _height(lines: list[str], key: int, typ: _Type) -> float:
    """Высота блока в долях кегля: от верха прописных первой строки до низа последней."""
    total = 0.0
    for i, raw in enumerate(lines):
        name, scale = _line_font(typ, i == key, raw)
        total += _cap_ratio(name) * scale
        if i:
            total += typ.gap + (typ.key_drop if i - 1 == key else 0.0)
    return total + _tail(_cased(lines[-1], len(lines) - 1 == key, typ)) * scale


def _place(lines: list[str], key: int, typ: _Type, size: int, zone: tuple[float, ...]) -> _Block:
    zx0, zy0, zx1, zy1 = zone
    pl, pt, pr, pb = (p * size for p in typ.pad)
    rows = []
    for i, raw in enumerate(lines):
        name, scale = _line_font(typ, i == key, raw)
        px = max(1, round(size * scale))
        text = _cased(raw, i == key, typ)
        box = _ink(name, px, text)
        rows.append((text, _font(name, px), px, box, -_ink(name, px, "Н")[1]))

    height = sum(r[4] for r in rows) + _tail(rows[-1][0]) * rows[-1][2]
    for i in range(1, len(rows)):
        height += (typ.gap + (typ.key_drop if i - 1 == key else 0.0)) * size
    if typ.valign == "bottom":
        top = zy1 - pb - height
    else:
        top = (zy0 + pt + zy1 - pb - height) / 2

    out: list[_Line] = []
    y = top
    for i, (text, font, px, box, cap) in enumerate(rows):
        if i:
            y += (typ.gap + (typ.key_drop if i - 1 == key else 0.0)) * size
        y += cap
        w = box[2] - box[0]
        x = zx0 + pl if typ.align == "left" else (zx0 + pl + zx1 - pr - w) / 2
        out.append(
            _Line(
                text=text,
                font=font,
                size=px,
                x=x,
                base=y,
                w=w,
                cap=cap,
                bearing=box[0],
                key=i == key,
            )
        )
    return _Block(
        lines=tuple(out),
        size=size,
        x0=min(ln.x for ln in out),
        y0=top,
        x1=max(ln.x + ln.w for ln in out),
        y1=top + height,
        zone=(zx0, zy0, zx1, zy1),
    )


def _layout(text: str, typ: _Type, width: int, height: int) -> _Block | None:
    """Строки, кегль и координаты надписи: самый крупный кегль, который влезает.

    Перебираются разбивки на 1…max_lines строк; для каждой кегль ограничен
    шириной зоны, её высотой и потолком стиля. Побеждает самый крупный, при
    равенстве — меньше строк. Одно очень длинное слово просто ужимается.
    """
    words = text.split()
    if not words:
        return None
    s = width / 1080
    zone = (typ.box[0] * width, typ.box[1] * height, typ.box[2] * width, typ.box[3] * height)
    room_w = zone[2] - zone[0]
    room_h = zone[3] - zone[1]
    pad_w = typ.pad[0] + typ.pad[2]
    pad_h = typ.pad[1] + typ.pad[3]

    units = _units(words)
    key_unit = _key_unit(units)
    best: tuple[float, list[str], int] | None = None
    for k in range(1, min(typ.max_lines, len(units)) + 1):
        lines, key, widest = _split(units, k, key_unit, typ)
        size = min(
            typ.max_size * s,
            room_w / (widest + pad_w),
            room_h / (_height(lines, key, typ) + pad_h),
        )
        if best is None or size > best[0] * 1.02:
            best = (size, lines, key)
    assert best is not None
    size, lines, key = best

    # Обмер шёл на эталонном кегле, а хинтинг на настоящем чуть другой: проверяем
    # по факту и ужимаем, пока не влезет.
    px = max(4, int(size))
    while True:
        block = _place(lines, key, typ, px, zone)
        fits_w = (block.x1 - block.x0) + pad_w * px <= room_w + 0.5
        fits_h = (block.y1 - block.y0) + pad_h * px <= room_h + 0.5
        if (fits_w and fits_h) or px <= 4:
            return block
        px -= 1


# ---------------------------------------------------------------- приёмы рисования

_WHITE = (255, 255, 255)
_INK = (17, 17, 17)


def _put(
    draw: ImageDraw.ImageDraw,
    ln: _Line,
    fill,  # noqa: ANN001 — цвет или яркость маски
    *,
    stroke: int = 0,
    stroke_fill=None,  # noqa: ANN001
) -> None:
    draw.text(
        (ln.x - ln.bearing, ln.base),
        ln.text,
        font=ln.font,
        fill=fill,
        anchor="ls",
        stroke_width=stroke,
        stroke_fill=stroke_fill,
    )


def _tone(
    img: Image.Image, *, contrast: float = 1.0, saturation: float = 1.0, brightness: float = 1.0
) -> Image.Image:
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    return img


def _tint(img: Image.Image, color: tuple[int, int, int], amount: float) -> Image.Image:
    return Image.blend(img, Image.new("RGB", img.size, color), amount)


def _vignette(img: Image.Image, strength: float) -> Image.Image:
    """Мягкое затемнение к краям; центр — там, где кадрирование ставит лицо."""
    w, h = img.size
    ys, xs = np.mgrid[0:96, 0:54]
    r = np.hypot((xs / 53 - 0.5) / 0.62, (ys / 95 - 0.42) / 0.72)
    fall = 1.0 - strength * np.clip((r - 0.45) / 0.75, 0.0, 1.0) ** 1.6
    mask = Image.fromarray((fall * 255).astype(np.uint8)).resize((w, h), Image.Resampling.BICUBIC)
    return ImageChops.multiply(img, Image.merge("RGB", (mask, mask, mask)))


def _ramp(
    size: tuple[int, int], start: float, end: float, top: float, power: float = 1.0
) -> Image.Image:
    """Вертикальная маска: 0 выше `start`, `top` (0…1) ниже `end`."""
    w, h = size
    ys = (np.arange(h, dtype=np.float64) / max(1, h - 1) - start) / max(1e-6, end - start)
    column = (np.clip(ys, 0.0, 1.0) ** power * top * 255).astype(np.uint8)
    return Image.fromarray(np.repeat(column[:, None], w, axis=1))


def _scrim(
    img: Image.Image,
    strength: float,
    *,
    start: float = _FACE_ZONE,
    end: float = 1.0,
    color: tuple[int, int, int] = (0, 0, 0),
    power: float = 1.0,
) -> Image.Image:
    """Затемнение низа под надпись. Начинается не выше границы зоны лица."""
    out = img.copy()
    out.paste(color, (0, 0), _ramp(img.size, start, end, strength, power))
    return out


def _text_mask(
    size: tuple[int, int], lines: Iterable[_Line], *, stroke: float = 0.0
) -> Image.Image:
    """Силуэт строк (с обводкой в долях кегля) — заготовка для тени."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for ln in lines:
        _put(draw, ln, 255, stroke=round(stroke * ln.size), stroke_fill=255)
    return mask


def _shade(
    img: Image.Image,
    mask: Image.Image,
    *,
    blur: float,
    strength: float,
    offset: tuple[float, float] = (0.0, 0.0),
    color: tuple[int, int, int] = (0, 0, 0),
) -> Image.Image:
    """Тень по готовой маске: сдвинуть, размыть, наложить цветом."""
    if offset != (0.0, 0.0):
        moved = Image.new("L", mask.size, 0)
        moved.paste(mask, (round(offset[0]), round(offset[1])))
        mask = moved
    if blur > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
    if strength != 1.0:
        mask = mask.point(lambda v: min(255, int(v * strength)))
    out = img.copy()
    out.paste(color, (0, 0), mask)
    return out


def _over(img: Image.Image, layer: Image.Image) -> Image.Image:
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


# ----------------------------------------------------------------------------- стили
#
# У каждого стиля две функции: `grade` красит кадр (она же работает, когда надписи
# нет) и `draw` кладёт надпись по готовой вёрстке. Затемнение низа под текст —
# часть надписи, а не цветокора: без надписи оно бы просто портило кадр.

_YELLOW = (255, 214, 10)


def _grade_plate(img: Image.Image) -> Image.Image:
    return _vignette(_tone(img, contrast=1.08, saturation=1.10), 0.22)


def _draw_plate(img: Image.Image, b: _Block) -> Image.Image:
    """Белые прописные, ключевая строка — чёрным на жёлтой плашке."""
    img = _scrim(img, 0.72, power=1.35)
    plain = [ln for ln in b.lines if not ln.key]
    shadow = _text_mask(img.size, plain)
    img = _shade(img, shadow, blur=0.10 * b.size, strength=0.75, offset=(0, 0.05 * b.size))
    k = b.key
    px, py = 0.22 * k.size, 0.19 * k.size
    plate = Image.new("L", img.size, 0)
    rect = (k.x - px, k.base - k.cap - py, k.x + k.w + px, k.base + py)
    ImageDraw.Draw(plate).rounded_rectangle(rect, radius=0.10 * k.size, fill=255)
    img = _shade(img, plate, blur=0.12 * b.size, strength=0.55, offset=(0, 0.07 * b.size))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(rect, radius=0.10 * k.size, fill=_YELLOW)
    for ln in b.lines:
        _put(draw, ln, _INK if ln.key else _WHITE)
    return img


_RED = (226, 27, 44)


def _grade_tape(img: Image.Image) -> Image.Image:
    return _vignette(_tone(img, contrast=1.12, saturation=0.92), 0.25)


def _draw_tape(img: Image.Image, b: _Block) -> Image.Image:
    """Каждая строка на своей красной ленте, весь блок слегка повёрнут."""
    img = _scrim(img, 0.42)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for ln in b.lines:
        px, py = 0.26 * ln.size, 0.17 * ln.size
        rect = (ln.x - px, ln.base - ln.cap - py, ln.x + ln.w + px, ln.base + py)
        draw.rectangle(rect, fill=(*_WHITE, 255) if ln.key else (*_RED, 255))
        _put(draw, ln, (*_RED, 255) if ln.key else (*_WHITE, 255))
    center = ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)
    layer = layer.rotate(3.0, resample=Image.Resampling.BICUBIC, center=center)
    img = _shade(
        img,
        layer.getchannel("A"),
        blur=0.14 * b.size,
        strength=0.6,
        offset=(0, 0.09 * b.size),
    )
    return _over(img, layer)


def _grade_outline(img: Image.Image) -> Image.Image:
    return _tone(img, contrast=1.10, saturation=1.22)


def _draw_outline(img: Image.Image, b: _Block) -> Image.Image:
    """Тяжёлый белый курсив с толстой чёрной обводкой и тенью; ключевая строка жёлтая."""
    img = _scrim(img, 0.35)
    stroke = 0.085
    solid = _text_mask(img.size, b.lines, stroke=stroke)
    img = _shade(img, solid, blur=0.16 * b.size, strength=0.7, offset=(0, 0.10 * b.size))
    img = _shade(img, solid, blur=0, strength=1.0, offset=(0.045 * b.size, 0.065 * b.size))
    draw = ImageDraw.Draw(img)
    for ln in b.lines:
        _put(
            draw,
            ln,
            (255, 226, 0) if ln.key else _WHITE,
            stroke=round(stroke * ln.size),
            stroke_fill=(0, 0, 0),
        )
    return img


_NEON_PINK = (255, 38, 150)
_NEON_CYAN = (0, 214, 255)


def _grade_neon(img: Image.Image) -> Image.Image:
    img = _tone(img, contrast=1.10, saturation=0.85, brightness=0.62)
    return _vignette(_tint(img, (26, 8, 64), 0.22), 0.45)


def _draw_neon(img: Image.Image, b: _Block) -> Image.Image:
    """Светящиеся трубки: белая сердцевина и цветной ореол на затемнённом кадре."""
    img = _scrim(img, 0.55, color=(10, 4, 30))
    glow = Image.new("RGB", img.size, (0, 0, 0))
    draw = ImageDraw.Draw(glow)
    for ln in b.lines:
        color = _NEON_CYAN if ln.key else _NEON_PINK
        _put(draw, ln, color, stroke=round(0.035 * ln.size), stroke_fill=color)
    img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(0.26 * b.size)))
    img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(0.09 * b.size)))
    img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(0.025 * b.size)))
    draw = ImageDraw.Draw(img)
    for ln in b.lines:
        _put(draw, ln, (238, 253, 255) if ln.key else (255, 240, 250))
    return img


_CORAL = (255, 77, 46)


def _grade_gradient(img: Image.Image) -> Image.Image:
    return _tone(img, contrast=1.05, saturation=1.04)


def _draw_gradient(img: Image.Image, b: _Block) -> Image.Image:
    """Тёмный градиент снизу, чистый белый текст по левому краю и акцентная черта."""
    img = _scrim(img, 0.96, end=0.93, power=1.3)
    draw = ImageDraw.Draw(img)
    bar_h = max(2.0, 0.085 * b.size)
    bar_y = b.y0 - 0.34 * b.size
    draw.rectangle((b.zone[0], bar_y - bar_h, b.zone[0] + 1.25 * b.size, bar_y), fill=_CORAL)
    for ln in b.lines:
        _put(draw, ln, _WHITE)
    return img


_CREAM = (244, 236, 220)
_PAPER_INK = (28, 26, 23)
_PAPER_RED = (170, 34, 28)


def _grade_paper(img: Image.Image) -> Image.Image:
    img = _tone(img, contrast=1.06, saturation=0.45)
    return _vignette(_tint(img, (150, 120, 84), 0.12), 0.25)


def _draw_paper(img: Image.Image, b: _Block) -> Image.Image:
    """Газетный заголовок: антиква на кремовой плашке с двойными линейками."""
    img = _scrim(img, 0.38)
    # Поля плашки не мельче, чем у кегля 84: на мелком наборе линейки иначе
    # прилипают к буквам.
    unit = max(float(b.size), 84.0 * img.width / 1080)
    px, py = 0.50 * unit, 0.62 * unit
    rect = (b.x0 - px, b.y0 - py, b.x1 + px, b.y1 + py - 0.10 * b.size)
    plate = Image.new("L", img.size, 0)
    ImageDraw.Draw(plate).rectangle(rect, fill=255)
    img = _shade(img, plate, blur=0.22 * b.size, strength=0.6, offset=(0, 0.10 * b.size))
    draw = ImageDraw.Draw(img)
    draw.rectangle(rect, fill=_CREAM)
    inset = 0.24 * unit
    thick, thin = max(2.0, 0.05 * unit), max(1.0, 0.016 * unit)
    x0, x1 = rect[0] + inset, rect[2] - inset
    top, bottom = rect[1] + inset, rect[3] - inset
    draw.rectangle((x0, top, x1, top + thick), fill=_PAPER_INK)
    draw.rectangle((x0, top + thick + 2 * thin, x1, top + thick + 3 * thin), fill=_PAPER_INK)
    draw.rectangle((x0, bottom - thick, x1, bottom), fill=_PAPER_INK)
    draw.rectangle((x0, bottom - thick - 3 * thin, x1, bottom - thick - 2 * thin), fill=_PAPER_INK)
    for ln in b.lines:
        _put(draw, ln, _PAPER_RED if ln.key else _PAPER_INK)
    return img


def _grade_duotone(img: Image.Image) -> Image.Image:
    """Карта градиента по яркости: тени в фиолетовый, света в оранжевый."""
    base = _tone(img, contrast=1.15)
    lum = np.asarray(base.convert("L"), dtype=np.uint8)
    stops = np.array([[38, 12, 92], [196, 48, 122], [255, 176, 84]], dtype=np.float64)
    pos = np.linspace(0.0, 1.0, 256)
    lut = np.stack([np.interp(pos, [0.0, 0.55, 1.0], stops[:, c]) for c in range(3)], axis=1)
    mapped = Image.fromarray(lut.astype(np.uint8)[lum])
    return _vignette(Image.blend(base, mapped, 0.74), 0.20)


def _draw_duotone(img: Image.Image, b: _Block) -> Image.Image:
    """Белый широкий гротеск на тонированном кадре."""
    img = _scrim(img, 0.62, color=(24, 6, 60))
    img = _shade(
        img,
        _text_mask(img.size, b.lines),
        blur=0.12 * b.size,
        strength=0.8,
        offset=(0, 0.06 * b.size),
        color=(24, 6, 60),
    )
    draw = ImageDraw.Draw(img)
    for ln in b.lines:
        _put(draw, ln, (255, 205, 112) if ln.key else _WHITE)
    return img


_MARKER = (255, 225, 77)


def _grade_marker(img: Image.Image) -> Image.Image:
    return _vignette(_tone(img, contrast=1.06, saturation=1.06), 0.30)


def _draw_marker(img: Image.Image, b: _Block) -> Image.Image:
    """Строгий гротеск и одна строка «от руки» акцентным цветом, с росчерком."""
    img = _scrim(img, 0.76, power=1.35)
    k = b.key
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    _put(draw, k, (*_MARKER, 255))
    # Росчерк под строкой: дуга переменной толщины, как от маркера с нажимом.
    y = k.base + 0.23 * k.size
    upper, lower = [], []
    steps = 24
    for i in range(steps + 1):
        u = i / steps
        x = k.x - 0.02 * k.w + u * 1.04 * k.w
        bend = -0.035 * k.size * math.sin(math.pi * u)
        thick = k.size * (0.012 + 0.036 * math.sin(math.pi * min(1.0, u * 1.35)) ** 0.8)
        upper.append((x, y + bend - thick))
        lower.append((x, y + bend + thick))
    draw.polygon(upper + lower[::-1], fill=(*_MARKER, 255))
    # Наклон тем меньше, чем длиннее строка: её концы не должны задевать соседей.
    angle = min(2.5, math.degrees(math.asin(min(1.0, 0.22 * k.size / max(1.0, k.w)))))
    layer = layer.rotate(
        angle, resample=Image.Resampling.BICUBIC, center=(k.x + k.w / 2, k.base - k.cap / 2)
    )
    plain = [ln for ln in b.lines if not ln.key]
    shadow = ImageChops.lighter(_text_mask(img.size, plain), layer.getchannel("A"))
    img = _shade(img, shadow, blur=0.11 * b.size, strength=0.8, offset=(0, 0.05 * b.size))
    draw = ImageDraw.Draw(img)
    for ln in plain:
        _put(draw, ln, _WHITE)
    return _over(img, layer)


@dataclass(frozen=True)
class _Look:
    style: CoverStyle
    type: _Type
    grade: Callable[[Image.Image], Image.Image]
    draw: Callable[[Image.Image, _Block], Image.Image]


_LOOKS: tuple[_Look, ...] = (
    _Look(
        CoverStyle("plate", {"ru": "Жёлтая плашка", "en": "Yellow plate"}),
        _Type(font="Montserrat-Black", max_size=230, gap=0.40, pad=(0.24, 0.22, 0.24, 0.24)),
        _grade_plate,
        _draw_plate,
    ),
    _Look(
        CoverStyle("tape", {"ru": "Красные ленты", "en": "Red tape"}),
        _Type(
            font="Oswald-SemiBold",
            max_size=220,
            gap=0.40,
            pad=(0.28, 0.19, 0.28, 0.24),
            box=(0.10, 0.585, 0.90, 0.855),
        ),
        _grade_tape,
        _draw_tape,
    ),
    _Look(
        CoverStyle("outline", {"ru": "Контур", "en": "Bold outline"}),
        _Type(font="Rubik-BlackItalic", max_size=230, gap=0.34, pad=(0.10, 0.10, 0.15, 0.20)),
        _grade_outline,
        _draw_outline,
    ),
    _Look(
        CoverStyle("neon", {"ru": "Неон", "en": "Neon"}),
        _Type(font="Montserrat-Bold", max_size=200, gap=0.42, pad=(0.08, 0.10, 0.08, 0.10)),
        _grade_neon,
        _draw_neon,
    ),
    _Look(
        CoverStyle("gradient", {"ru": "Градиент", "en": "Clean gradient"}),
        _Type(
            font="Montserrat-ExtraBold",
            upper=False,
            max_size=190,
            gap=0.38,
            align="left",
            valign="bottom",
            pad=(0.0, 0.46, 0.0, 0.0),
            box=(0.08, 0.56, 0.92, 0.865),
        ),
        _grade_gradient,
        _draw_gradient,
    ),
    _Look(
        CoverStyle("paper", {"ru": "Газета", "en": "Newspaper"}),
        _Type(
            font="YesevaOne-Regular",
            key_font="PlayfairDisplay-ExtraBoldItalic",
            key_font_plain_digits=True,
            max_size=180,
            gap=0.36,
            pad=(0.50, 0.62, 0.50, 0.62),
        ),
        _grade_paper,
        _draw_paper,
    ),
    _Look(
        CoverStyle("duotone", {"ru": "Дуотон", "en": "Duotone"}),
        _Type(font="Unbounded-ExtraBold", max_size=170, gap=0.40, pad=(0.04, 0.06, 0.04, 0.12)),
        _grade_duotone,
        _draw_duotone,
    ),
    _Look(
        CoverStyle("marker", {"ru": "От руки", "en": "Handwritten"}),
        _Type(
            font="Montserrat-ExtraBold",
            key_font="Caveat-Bold",
            key_scale=1.75,
            key_case="lower",
            max_size=190,
            gap=0.36,
            key_drop=0.30,
            pad=(0.06, 0.12, 0.06, 0.20),
        ),
        _grade_marker,
        _draw_marker,
    ),
)

STYLES: tuple[CoverStyle, ...] = tuple(look.style for look in _LOOKS)


def style_keys() -> list[str]:
    return [s.key for s in STYLES]


def _look(style: str) -> _Look:
    for look in _LOOKS:
        if look.style.key == style:
            return look
    return _LOOKS[0]  # незнакомый ключ — первый стиль, а не ошибка


def _caption_block(text: str, style: str, width: int = 1080, height: int = 1920) -> _Block | None:
    """Вёрстка надписи без рисования — для тестов и отладки."""
    look = _look(style)
    return _layout(_clean_text(text, look.type.fonts()), look.type, width, height)


# ------------------------------------------------------------------------- сборка


def compose(
    image: np.ndarray, text: str, style: str, *, width: int = 1080, height: int = 1920
) -> Image.Image:
    """Обложка: BGR-картинка, кадрированная в 9:16, с цветокором стиля и надписью.

    Пустая надпись — просто кадр в цветокоре стиля. Незнакомый стиль — первый.
    """
    look = _look(style)
    base = look.grade(_frame_photo(image, width, height)).convert("RGB")
    with _type_lock:
        block = _layout(_clean_text(text, look.type.fonts()), look.type, width, height)
        if block is None:
            return base
        captioned = look.draw(base, block).convert("RGB")
    # Зона лица неприкосновенна: выше границы всегда чистый кадр.
    guard = _ramp((width, height), _FACE_ZONE, _FACE_ZONE + _FACE_ZONE_FADE, 1.0)
    return Image.composite(captioned, base, guard)


def make_cover(src: Path, out: Path, *, text: str, style: str, from_video: bool) -> None:
    """Собрать обложку из видео или фото и записать JPEG 1080×1920 в `out`."""
    src, out = Path(src), Path(out)
    image = pick_frame(src) if from_video else read_image(src)
    cover = compose(image, text, style)
    buf = io.BytesIO()
    cover.save(buf, "JPEG", quality=90, optimize=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Через временный файл: читатель не должен застать половину JPEG.
    part = out.with_name(out.name + ".part")
    part.write_bytes(buf.getvalue())
    os.replace(part, out)


# ------------------------------------------------------------------ фон для превью


def sample_background(width: int, height: int) -> np.ndarray:
    """Нарисованный фон для превью стилей: студийный градиент и силуэт по плечи.

    Никаких фотографий: превью показывает стиль, а не человека. Силуэт стоит там
    же, куда кадрирование ставит лицо, — видно, что надпись его не закрывает.
    """
    width, height = max(2, int(width)), max(2, int(height))
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    u, v = xs / (width - 1), ys / (height - 1)
    # Тёплая студия: пятно света за головой, к краям и вниз темнее.
    glow = np.clip(1.0 - np.hypot((u - 0.5) / 0.85, (v - 0.33) / 0.62), 0.0, 1.0) ** 1.4
    light = np.array([240, 214, 184], dtype=np.float64)
    dark = np.array([138, 92, 74], dtype=np.float64)
    wall = dark + (light - dark) * glow[..., None]
    wall *= (1.0 - 0.22 * np.clip((v - 0.55) / 0.45, 0.0, 1.0))[..., None]
    img = Image.fromarray(wall.clip(0, 255).astype(np.uint8))

    ss = 2  # рисуем вдвое крупнее и уменьшаем — края эллипсов без лесенки
    w2, h2 = width * ss, height * ss
    figure = Image.new("RGBA", (w2, h2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(figure)

    def oval(cx: float, cy: float, rx: float, ry: float, color: tuple[int, int, int]) -> None:
        draw.ellipse(
            ((cx - rx) * w2, (cy - ry) * h2, (cx + rx) * w2, (cy + ry) * h2), fill=(*color, 255)
        )

    oval(0.5, 0.93, 0.62, 0.365, (46, 58, 82))  # плечи
    draw.rounded_rectangle(
        (0.395 * w2, 0.42 * h2, 0.605 * w2, 0.60 * h2), radius=0.07 * w2, fill=(168, 116, 96, 255)
    )  # шея
    oval(0.5, 0.588, 0.135, 0.034, (168, 116, 96))  # вырез
    # Голова — гладкий овал без волос и черт. Так задумано: с «причёской» детектор
    # лиц принимал силуэт за лицо (оценка 0.63 при пороге 0.6), и превью стилей
    # зависело бы от того, в какую сторону округлится эта оценка на другой машине.
    oval(0.5, 0.380, 0.190, 0.130, (192, 138, 112))  # голова
    figure = figure.resize((width, height), Image.Resampling.LANCZOS)

    shadow = figure.getchannel("A").filter(ImageFilter.GaussianBlur(max(1.0, width * 0.08)))
    img.paste((92, 58, 48), (0, 0), shadow.point(lambda a: int(a * 0.20)))
    img = Image.alpha_composite(img.convert("RGBA"), figure).convert("RGB")
    # Размывается готовая картинка, а не слой фигуры: у полупрозрачного слоя края
    # при размытии темнеют, и вокруг головы получался контур.
    img = img.filter(ImageFilter.GaussianBlur(max(1.0, width * 0.011)))
    return np.ascontiguousarray(np.asarray(img, dtype=np.uint8)[:, :, ::-1])
