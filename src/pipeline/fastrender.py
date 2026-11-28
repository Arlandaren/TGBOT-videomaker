"""Быстрый рендер без браузера: камера, переходы, субтитры и полоса — в ffmpeg.

Зачем. Remotion снимает каждый кадр скриншотом Chromium. Замер 2026-11-28 на
старом сервере (план 30 с, 900 кадров, 3 ядра): полный рендер — 142 с; тот же
план БЕЗ видео, одни надписи и графика на прозрачном фоне — 125 с; одни кадры
браузера без кодирования — 106 с с видео и 69 с без него. То есть дорог сам
кадр браузера, а не видео в нём и не графика: ускорить рендер, оставив браузер,
нельзя. Субтитры через libass на том же плане — 46 с, и большую часть из них
занимал кодек на пресете medium.

Как устроено. Три процесса цепочкой:

    ffmpeg (декод, yuv420p) → Python (камера, переходы, вспышка, полоса)
                            → ffmpeg (субтитры libass, звук, x264)

Движение кадра считается теми же формулами, что в браузере: камера —
`camera.state_at` (зеркало `Camera.ts`), переходы — `cut_at` (зеркало `cutAt`
из `render/src/visuals/motion.mjs`, константы сверяет тест). Субтитры —
`subs_ass` с кеглем и местом блока из плана.

Чего здесь НЕТ (план с ними уходит в Remotion, см. `supports`): графика
(`meta.visuals`: стикеры, плашки, декор, сцены), b-roll, стеклянная панель
субтитров, титр за головой. Субтитры проще, чем в браузере: нет пружин и
появления блока, «пшик» слова — масштабом libass.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from src.core.camera import state_at
from src.core.config import Settings, settings as default_settings
from src.core.render_plan import RenderPlan

log = logging.getLogger(__name__)

# Константы переходов — копия `motion.mjs` (тест `test_fastrender` читает их
# оттуда и сверяет: разойтись молча они не могут).
MIN_ANIM_FRAMES = 8
CUT_PUNCH = 0.12
CUT_SLIDE_RATIO = 0.07
CUT_SHAKE_RATIO = 0.012
CUT_SHAKE_TURNS = 3
CUT_FLASH = 0.75
CUT_COVER = 0.01
TAU = math.pi * 2


class FastRenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Motion:
    """Кадр видео на кадре ролика: `x' = scale·x + tx`, плюс белая вспышка."""

    scale: float = 1.0
    tx: float = 0.0
    ty: float = 0.0
    flash: float = 0.0

    @property
    def still(self) -> bool:
        return self.scale == 1.0 and self.tx == 0.0 and self.ty == 0.0


# Запас кадров вокруг окна слоя: появление начинается на кадр раньше, а уход
# субтитра (`blockOutFrames`) и вставки живёт после конца. Лишний кадр стоит
# только времени браузера, недостающий — обрезанной анимации.
LEAD_FRAMES = 2
TAIL_FRAMES = 18
# Если браузеру всё равно снимать большую часть кадров целиком, быстрый путь
# не быстрее обычного рендера: такой ролик уходит в Remotion целиком.
MAX_UNDER_SHARE = 0.6


@dataclass(frozen=True, slots=True)
class BrowserWork:
    """Что достаётся браузеру: номера «его» блоков и кадры двух проходов."""

    blocks: tuple[int, ...] = ()
    under: tuple[int, ...] = ()  # целый кадр: слою нужны пиксели видео
    overlay: tuple[int, ...] = ()  # прозрачный слой поверх кадра ffmpeg

    @property
    def frames(self) -> int:
        return len(self.under) + len(self.overlay)


def _browser_block(b: Any) -> bool:
    """Блок, который libass так же не нарисует: заголовок, плашка, значок,
    панель, уход за голову, эмодзи (цветных глифов в libass нет)."""
    if b.title_card or b.pill or b.icon or b.behind or b.panel:
        return True
    return any(w.emoji for w in b.words)


def _span(start: float, end: float, fps: float, total: int, tail: int = TAIL_FRAMES) -> range:
    first = max(0, _js_round(start * fps) - LEAD_FRAMES)
    last = min(total - 1, _js_round(end * fps) + tail)
    return range(first, last + 1)


def browser_work(plan: RenderPlan, total: int, *, all_captions: bool = False) -> BrowserWork:
    """Какие кадры и какие слои снимет браузер.

    `all_captions` — все субтитры рисует браузер (режим `hybrid`): вид ровно
    прежний, libass не участвует, а ffmpeg остаётся видео, камера и переходы.
    """
    fps = plan.fps
    blocks = tuple(
        i for i, b in enumerate(plan.blocks) if all_captions or _browser_block(b)
    )
    visuals = plan.meta.get("visuals") or {}
    under: set[int] = set()
    for item in plan.broll:
        under.update(_span(item.start, item.end, fps, total))
    for scene in visuals.get("scenes") or []:
        under.update(_span(float(scene.get("start", 0)), float(scene.get("end", 0)), fps, total))
    for i in blocks:
        if plan.blocks[i].panel:  # стекло размывает видео под собой
            under.update(_span(plan.blocks[i].start, plan.blocks[i].end, fps, total))
    overlay: set[int] = set()
    for item in visuals.get("overlays") or []:
        out = int(item.get("out_frames") or 0)
        overlay.update(_span(float(item.get("start", 0)), float(item.get("end", 0)), fps, total,
                             tail=max(TAIL_FRAMES, out + 2)))
    for i in blocks:
        overlay.update(_span(plan.blocks[i].start, plan.blocks[i].end, fps, total))
    return BrowserWork(blocks=blocks, under=tuple(sorted(under)),
                       overlay=tuple(sorted(overlay - under)))


def supports(plan: RenderPlan, *, all_captions: bool = False) -> tuple[bool, str]:
    """Стоит ли отдавать план быстрому рендеру и, если нет, почему.

    Почти всё, чего не умеет ffmpeg, браузер дорисует на своих кадрах
    (`browser_work`). Невыгодно одно: когда браузеру всё равно снимать
    целиком большую часть ролика (стеклянная панель у каждого блока, b-roll
    на полролика) — тогда обычный рендер честнее и не медленнее.
    """
    total = max(1, _js_round(plan.duration * plan.fps))
    work = browser_work(plan, total, all_captions=all_captions)
    if len(work.under) > MAX_UNDER_SHARE * total:
        return False, f"браузеру нужен целый кадр на {len(work.under) * 100 // total} % ролика"
    return True, ""


def _js_round(x: float) -> int:
    """`Math.round` из JS: половина — вверх, а не к чётному, как `round` в Python."""
    return math.floor(x + 0.5)


def _ease_out(t: float) -> float:
    x = 1 - min(1.0, max(0.0, t))
    return 1 - x * x * x


def cut_at(cuts: Any, frame: int, fps: float, width: int, height: int) -> tuple[float, float, float, float]:
    """`(scale, dx, dy, flash)` перехода на кадре. Зеркало `cutAt` из motion.mjs."""
    if not isinstance(cuts, list):
        return 1.0, 0.0, 0.0, 0.0
    for cut in cuts:
        if not isinstance(cut, dict):
            continue
        f0 = _js_round(float(cut.get("at", 0)) * fps)
        frames = max(MIN_ANIM_FRAMES, _js_round(float(cut.get("frames") or 0)))
        if not f0 <= frame < f0 + frames:
            continue
        t = (frame - f0) / frames
        k = 1 - _ease_out(t)
        kind = cut.get("kind")
        if kind == "punch":
            return 1 + CUT_PUNCH * k, 0.0, 0.0, 0.0
        if kind in ("slide_left", "slide_right"):
            side = 1 if kind == "slide_left" else -1
            return 1 + (2 * CUT_SLIDE_RATIO + CUT_COVER) * k, side * CUT_SLIDE_RATIO * width * k, 0.0, 0.0
        if kind == "shake":
            reach = CUT_SHAKE_RATIO * width * k
            return (
                1 + (2 * CUT_SHAKE_RATIO + CUT_COVER) * k,
                reach * math.sin(TAU * CUT_SHAKE_TURNS * t),
                0.6 * reach * math.sin(TAU * CUT_SHAKE_TURNS * 1.3 * t + 1.1),
                0.0,
            )
        if kind == "flash":
            return 1.0, 0.0, 0.0, CUT_FLASH * k
    return 1.0, 0.0, 0.0, 0.0


def motion_at(plan: RenderPlan, frame: int) -> Motion:
    """Камера внутри перехода, как вложены слои в `Reel.tsx`.

    Камера: `p' = o + s1·(p − o)` (CSS `scale` с `transform-origin` в точке
    плана). Переход поверх неё: `p'' = c + d + s2·(p' − c)`, где `c` — центр
    кадра (`translate(d) scale(s2)` с началом в центре).
    """
    shots = [SimpleNamespace(**s) for s in (plan.meta.get("camera") or []) if isinstance(s, dict)]
    s1, ox, oy = state_at(shots, frame, plan.fps) if shots else (1.0, 0.0, 0.0)
    s2, dx, dy, flash = cut_at(plan.meta.get("cuts"), frame, plan.fps, plan.width, plan.height)
    cx, cy = plan.width / 2, plan.height / 2
    return Motion(
        scale=s1 * s2,
        tx=s2 * ox * (1 - s1) + cx * (1 - s2) + dx,
        ty=s2 * oy * (1 - s1) + cy * (1 - s2) + dy,
        flash=flash,
    )


def _hex_to_yuv(color: str) -> tuple[int, int, int]:
    """Цвет `#RRGGBB` в Y'CbCr BT.709 ограниченного диапазона (как у видео)."""
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    u = (b - y) / 1.8556
    v = (r - y) / 1.5748
    return round(16 + 219 * y), round(128 + 224 * u), round(128 + 224 * v)


def _progress(plan: RenderPlan) -> tuple[int, tuple[int, int, int]] | None:
    raw = plan.meta.get("progress")
    if not isinstance(raw, dict):
        return None
    height, color = raw.get("height"), raw.get("color")
    if not isinstance(color, str) or not isinstance(height, (int, float)) or height <= 0:
        return None
    return int(round(height)), _hex_to_yuv(color)


class _Painter:
    """Двигает и докрашивает кадр yuv420p: сдвиг и масштаб — cv2, остальное — numpy."""

    def __init__(
        self, plan: RenderPlan, total: int
    ) -> None:
        import cv2

        self.cv2 = cv2
        self.plan = plan
        self.w, self.h = plan.width, plan.height
        self.total = max(1, total)
        self.bar = _progress(plan)

    def _rgb_to_yuv(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """RGB браузера → Y'CbCr BT.709 ограниченного диапазона, как у видео.

        Chromium сам декодирует видео по BT.709 (замер 2026-11-28: кадр
        браузера против кадра файла — расхождение меньше уровня яркости),
        поэтому обратный перевод той же матрицей сшивает кадры без скачка цвета.
        """
        x = rgb.astype(np.float32) * (1 / 255)
        r, g, b = x[..., 0], x[..., 1], x[..., 2]
        yy = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return (16 + 219 * yy,
                128 + 224 * (b - yy) / 1.8556,
                128 + 224 * (r - yy) / 1.5748)

    def _half(self, plane: np.ndarray) -> np.ndarray:
        return self.cv2.resize(plane, (plane.shape[1] // 2, plane.shape[0] // 2),
                               interpolation=self.cv2.INTER_AREA)

    def _read(self, path: Path, flags: int) -> np.ndarray | None:
        # `imdecode`, а не `imread`: OpenCV не открывает пути с кириллицей.
        data = np.fromfile(str(path), dtype=np.uint8)
        img = self.cv2.imdecode(data, flags) if data.size else None
        if img is None or img.shape[0] != self.h or img.shape[1] != self.w:
            log.warning("кадр браузера %s не прочитан — кадр без него", path.name)
            return None
        return img

    def prepare(self, kind: str, path: Path | None):
        """Кадр браузера → готовые к наложению плоскости. Идёт в потоке: cv2 и
        numpy отпускают GIL, и разбор PNG не тормозит основной цикл.

        `("under", (y, u, v))` — кадр целиком; `("overlay", (x0, y0, x1, y1,
        ly, lu, lv, a, a2))` — непустой прямоугольник слоя; `None` — нечего
        накладывать.
        """
        if path is None:
            return None
        if kind == "under":
            img = self._read(path, self.cv2.IMREAD_COLOR)
            if img is None:
                return None
            y, u, v = self._rgb_to_yuv(img[..., ::-1])
            return "under", (np.clip(y + 0.5, 0, 255).astype(np.uint8),
                             np.clip(self._half(u) + 0.5, 0, 255).astype(np.uint8),
                             np.clip(self._half(v) + 0.5, 0, 255).astype(np.uint8))
        img = self._read(path, self.cv2.IMREAD_UNCHANGED)
        if img is None or img.ndim != 3 or img.shape[2] != 4:
            return None
        alpha = img[..., 3]
        if not alpha.any():
            return None
        # Только прямоугольник, где слой не пуст: стикер — пара процентов
        # кадра. Края чётные: у цветности половинное разрешение.
        x0, y0, bw, bh = self.cv2.boundingRect(alpha)
        x0, y0 = x0 // 2 * 2, y0 // 2 * 2
        x1 = min(self.w, (x0 + bw + 2) // 2 * 2)
        y1 = min(self.h, (y0 + bh + 2) // 2 * 2)
        tile = img[y0:y1, x0:x1]
        a = tile[..., 3].astype(np.float32) * (1 / 255)
        ly, lu, lv = self._rgb_to_yuv(tile[..., 2::-1])
        return "overlay", (x0, y0, x1, y1, ly, self._half(lu), self._half(lv), a, self._half(a))

    @staticmethod
    def _blend(y, u, v, data):
        x0, y0, x1, y1, ly, lu, lv, a, a2 = data
        y, u, v = (p if p.flags.writeable else p.copy() for p in (y, u, v))
        ys = y[y0:y1, x0:x1].astype(np.float32)
        y[y0:y1, x0:x1] = np.clip(ys + (ly - ys) * a + 0.5, 0, 255).astype(np.uint8)
        cy0, cy1, cx0, cx1 = y0 // 2, y1 // 2, x0 // 2, x1 // 2
        for plane, layer in ((u, lu), (v, lv)):
            ps = plane[cy0:cy1, cx0:cx1].astype(np.float32)
            plane[cy0:cy1, cx0:cx1] = np.clip(ps + (layer - ps) * a2 + 0.5, 0, 255).astype(np.uint8)
        return y, u, v

    def paint(self, buf: np.ndarray, frame: int, layer=None) -> np.ndarray:
        cv2 = self.cv2
        w, h = self.w, self.h
        y = buf[: w * h].reshape(h, w)
        u = buf[w * h : w * h * 5 // 4].reshape(h // 2, w // 2)
        v = buf[w * h * 5 // 4 :].reshape(h // 2, w // 2)

        kind, data = layer if layer else (None, None)
        m = motion_at(self.plan, frame)
        if kind == "under":
            # Кадр целиком из браузера: камера, переход и вспышка в нём уже есть.
            y, u, v = data
        else:
            if not m.still:
                full = np.float32([[m.scale, 0, m.tx], [0, m.scale, m.ty]])
                half = np.float32([[m.scale, 0, m.tx / 2], [0, m.scale, m.ty / 2]])
                flags = cv2.INTER_LINEAR
                border = cv2.BORDER_REPLICATE
                y = cv2.warpAffine(y, full, (w, h), flags=flags, borderMode=border)
                u = cv2.warpAffine(u, half, (w // 2, h // 2), flags=flags, borderMode=border)
                v = cv2.warpAffine(v, half, (w // 2, h // 2), flags=flags, borderMode=border)
            if m.flash > 0:
                # Белый слой поверх видео с прозрачностью `flash` (`CutFlash`).
                a = m.flash
                y = (y * (1 - a) + 235 * a).astype(np.uint8)
                u = (u * (1 - a) + 128 * a).astype(np.uint8)
                v = (v * (1 - a) + 128 * a).astype(np.uint8)
        if kind == "overlay":
            y, u, v = self._blend(y, u, v, data)
        if self.bar:
            # Полоса прогресса (`ProgressBar`): во всю ширину, растёт слева.
            height, (by, bu, bv) = self.bar
            done = min(1.0, (frame + 1) / self.total)
            px = int(round(w * done)) // 2 * 2
            if px > 0:
                y, u, v = (p if p.flags.writeable else p.copy() for p in (y, u, v))
                y[:height, :px] = by
                u[: max(1, height // 2), : px // 2] = bu
                v[: max(1, height // 2), : px // 2] = bv
        return np.concatenate([y.reshape(-1), u.reshape(-1), v.reshape(-1)])


def _resolve(path: str | None, work_dir: Path) -> str | None:
    if not path:
        return None
    p = Path(path)
    return str(p if p.is_absolute() else work_dir / p)


def _filter_path(path: Path) -> str:
    """Путь внутри строки фильтра ffmpeg: двоеточия и кавычки экранируются."""
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


_FRAME_DONE = re.compile(r"\[render\] frame (under|overlay) (\d+)")


class _BrowserFeed:
    """Браузер снимает свои кадры ПАРАЛЛЕЛЬНО с конвейером ffmpeg.

    Замер 2026-11-28 (план 27.5 с, 442 кадра графики, 3 ядра): по очереди —
    браузер 34.5 с, потом конвейер 45 с. Конвейер ждёт кадр браузера, только
    когда до него дошёл, а `render.mjs` сообщает о каждом готовом кадре строкой
    `[render] frame <проход> <номер>` — файл к этому моменту уже записан.
    """

    def __init__(
        self, plan: RenderPlan, work: BrowserWork, work_dir: Path, cfg: Settings,
        on_progress: Callable[[float], None] | None,
    ) -> None:
        from src.pipeline import render as R

        self.work = work
        self.kind = {f: "under" for f in work.under} | {f: "overlay" for f in work.overlay}
        self.dirs = {"under": work_dir / "browser-under", "overlay": work_dir / "browser-overlay"}
        self.ready: dict[int, Path | None] = {}
        self.cond = threading.Condition()
        self.finished = False
        self.tail: list[str] = []
        self.on_progress = on_progress
        self.started = time.monotonic()

        staged = R.prepare_assets(replace(plan, meta=dict(plan.meta)), work_dir, cfg=cfg)
        req = R.RenderRequest(
            plan=staged, out=work_dir / "browser.unused.mp4", assets_dir=work_dir,
            job_key=f"{work_dir.name}-fast", crf=cfg.render_crf,
            concurrency=cfg.render_concurrency, x264_preset=cfg.render_x264_preset,
            gl=cfg.render_gl or None,
        )
        payload = json.loads(R._request_payload(req))
        passes = [
            {"layers": kind, "frames": list(frames), "dir": str(self.dirs[kind].resolve())}
            for kind, frames in (("under", work.under), ("overlay", work.overlay)) if frames
        ]
        # Проход, чьи кадры нужны конвейеру раньше, идёт первым.
        payload["passes"] = sorted(passes, key=lambda p: p["frames"][0])
        payload["browserBlocks"] = list(work.blocks)
        payload["totalFrames"] = work.frames
        self.proc = subprocess.Popen(
            ["node", str(R.RENDER_CLI)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False))
        self.proc.stdin.close()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _file(self, kind: str, frame: int) -> Path | None:
        folder = self.dirs[kind]
        ext = "png" if kind == "overlay" else "jpeg"
        for pad in {1, len(str(frame)), len(str(max(self.kind))), len(str(max(self.kind) + 1))}:
            p = folder / f"element-{frame:0{pad}d}.{ext}"
            if p.exists():
                return p
        hits = [p for p in folder.glob(f"element-*.{ext}") if p.stem.split("-", 1)[1].lstrip("0") == str(frame).lstrip("0")]
        return hits[0] if hits else None

    def _read(self) -> None:
        from src.pipeline import render as R

        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            m = _FRAME_DONE.search(line)
            if m:
                kind, frame = m.group(1), int(m.group(2))
                path = self._file(kind, frame)
                with self.cond:
                    self.ready[frame] = path
                    self.cond.notify_all()
                continue
            self.tail.append(line.rstrip())
            del self.tail[:-100]
            p = R._PROGRESS.search(line)
            if p and self.on_progress:
                self.on_progress(min(1.0, int(p.group(1)) / 100))
        self.proc.wait()
        with self.cond:
            self.finished = True
            self.cond.notify_all()

    def wait(self, frame: int) -> Path | None:
        """Файл кадра браузера; ждёт, пока он не снят. Не снялся — None."""
        with self.cond:
            while frame not in self.ready and not self.finished:
                self.cond.wait(timeout=5)
            path = self.ready.get(frame)
        if path is None and self.finished:
            # Строка о кадре могла не дойти: последняя попытка — по диску.
            path = self._file(self.kind[frame], frame)
        return path

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.reader.join(timeout=10)

    def check(self) -> None:
        if self.proc.returncode not in (0, None):
            raise FastRenderError("кадры браузера не сняты:\n" + "\n".join(self.tail[-30:]))


# Сколько кадров браузера разбирать заранее: столько потоков и держит пул.
PREFETCH = 6


def render_fast(
    plan: RenderPlan,
    out: str | Path,
    *,
    work_dir: str | Path,
    cfg: Settings | None = None,
    on_progress: Callable[[float], None] | None = None,
    all_captions: bool = False,
    crf: int | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Собрать ролик: кадр — ffmpeg, свои слои — браузер. Блокирующий вызов.

    `cancel` — выставленное событие обрывает сборку между кадрами: поток из
    `asyncio.to_thread` иначе не отменить, а выкладка ждёт конца задачи.
    """
    from concurrent.futures import Future, ThreadPoolExecutor

    from src.pipeline import fit
    from src.pipeline.subs_ass import font_em_scale, write_ass

    cfg = cfg or default_settings
    work_dir = Path(work_dir)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h, fps = plan.width, plan.height, plan.fps
    if w % 2 or h % 2:
        raise FastRenderError(f"кадр {w}x{h}: для yuv420p стороны должны быть чётными")
    total = max(1, _js_round(plan.duration * fps))

    video = _resolve(plan.video, work_dir)
    audio = _resolve(plan.audio, work_dir)
    if not video or not Path(video).exists():
        raise FastRenderError(f"нет видео плана: {plan.video}")

    work = browser_work(plan, total, all_captions=all_captions)

    # Блоки браузера libass не рисует: иначе они вышли бы дважды.
    own = set(work.blocks)
    simple = replace(plan, blocks=[b for i, b in enumerate(plan.blocks) if i not in own])
    font_file = cfg.fonts_dir / (plan.preset.get("font") or {}).get("file", "")
    em = font_em_scale(font_file)
    ass = write_ass(simple, work_dir / "captions.ass", em_scale=em, place=True)

    threads = ["-threads", str(cfg.ffmpeg_threads)] if cfg.ffmpeg_threads > 0 else []
    decode = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", *threads, "-i", video,
        # `cover`, как у <Video objectFit="cover">: файл другой пропорции не
        # растягивается, а заполняет кадр с обрезкой краёв.
        "-map", "0:v:0",
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps}",
        "-frames:v", str(total),
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1",
    ]
    vf = f"subtitles='{_filter_path(ass)}':fontsdir='{_filter_path(cfg.fonts_dir)}'"
    cap = fit.render_cap(plan.duration, cfg)
    rate = (
        ["-maxrate", f"{cap.video_kbps}k", "-bufsize", f"{cap.video_kbps * 2}k"] if cap else []
    )
    audio_kbps = (cap.audio_kbps if cap and cap.audio_kbps else 192)
    audio_in = ["-i", audio] if audio else ["-i", video]
    encode = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", *threads,
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-r", str(fps), "-i", "pipe:0",
        *audio_in, "-map", "0:v:0", "-map", "1:a:0?",
        "-vf", vf, "-c:v", "libx264", "-preset", cfg.fast_x264_preset,
        "-crf", str(cfg.render_crf if crf is None else crf),
        *rate, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-shortest", "-movflags", "+faststart", str(out),
    ]

    painter = _Painter(plan, total)
    size = w * h * 3 // 2
    started = time.monotonic()
    feed = _BrowserFeed(plan, work, work_dir, cfg, None) if work.frames else None
    pool = ThreadPoolExecutor(max_workers=PREFETCH) if feed else None
    pending: dict[int, Future] = {}

    def schedule(upto: int) -> None:
        if feed is None or pool is None:
            return
        for f in range(upto, min(total, upto + PREFETCH)):
            if f in feed.kind and f not in pending:
                pending[f] = pool.submit(
                    lambda f=f: painter.prepare(feed.kind[f], feed.wait(f))
                )

    dec = subprocess.Popen(decode, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    enc = subprocess.Popen(encode, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    errs: dict[str, bytes] = {}

    def drain(name: str, stream) -> None:  # stderr читаем сразу: полный буфер вешает ffmpeg
        errs[name] = stream.read()

    readers = [
        threading.Thread(target=drain, args=("dec", dec.stderr), daemon=True),
        threading.Thread(target=drain, args=("enc", enc.stderr), daemon=True),
    ]
    for r in readers:
        r.start()

    frame = 0
    missing = 0
    last = np.zeros(size, dtype=np.uint8)
    try:
        assert dec.stdout is not None and enc.stdin is not None
        while frame < total:
            if cancel is not None and cancel.is_set():
                raise FastRenderError("сборка отменена")
            schedule(frame)
            raw = dec.stdout.read(size)
            if len(raw) < size:
                # Видео на кадр-другой короче плана: держим последний кадр, как
                # держит его <Video> в браузере.
                if frame == 0:
                    break
                buf = last
            else:
                # Копия: буфер из `read` только для чтения, а полосу красим на месте.
                buf = np.frombuffer(raw, dtype=np.uint8).copy()
                last = buf
            layer = None
            if frame in pending:
                layer = pending.pop(frame).result()
                if layer is None and feed is not None and feed.kind[frame] == "under":
                    missing += 1
            enc.stdin.write(painter.paint(buf, frame, layer).tobytes())
            frame += 1
            if on_progress and frame % max(1, total // 50) == 0:
                on_progress(frame / total)
        enc.stdin.close()
    except BrokenPipeError:
        pass
    finally:
        dec.kill()
        dec.wait()
        enc.wait(timeout=600)
        for r in readers:
            r.join(timeout=5)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if feed is not None:
            feed.close()

    if feed is not None:
        feed.check()
    if frame == 0:
        raise FastRenderError(f"видео не прочиталось: {errs.get('dec', b'').decode(errors='replace')[-800:]}")
    if enc.returncode != 0 or not out.exists():
        raise FastRenderError(f"кодирование упало: {errs.get('enc', b'').decode(errors='replace')[-800:]}")
    if missing:
        log.warning("быстрый рендер: %d целых кадров браузера не снялись — там кадр ffmpeg", missing)
    seconds = time.monotonic() - started
    log.info(
        "быстрый рендер: %d кадров (браузер — %d) за %.1f с (%.2fx реального времени)",
        frame, work.frames, seconds, seconds / max(0.001, plan.duration),
    )
    return out
