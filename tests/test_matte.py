"""Маска человека для заголовка за головой: кадры, замер, файлы масок, отказы.

Модель здесь подменена: тесты проверяют всё вокруг неё — что читаются ровно
нужные кадры, что решение принимается по маске, что маски пишутся так, как их
читает рендерер, и что любой сбой означает обычный титр, а не упавшую задачу.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from src.core.behind import BehindDecision, TitleBoxes
from src.core.config import Settings
from src.core.edl import EDL, Broll, CaptionBlock, CaptionWord, Emphasis, Word
from src.core.geometry import Rect
from src.core.preset import load_preset
from src.core.track import FaceTrack, TrackPoint
from src.pipeline import matte

FRAME = (540, 960)
FPS = 30


def cfg(**kw) -> Settings:
    return Settings(_env_file=None, bot_token="x", sub_tail=0.15, render_fps=FPS, **kw)


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    """Две секунды, где яркость кадра равна его номеру × 3 — кадр узнаётся по картинке."""
    out = tmp_path_factory.mktemp("matte") / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=black:s={FRAME[0]}x{FRAME[1]}:r={FPS}:d=2,format=gray,geq=lum='N*3'",
         "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out


# ------------------------------------------------------------------ кадры


def test_exactly_the_asked_frames_are_read(clip):
    frames = matte.decode_span(clip, 6, 12, (64, 112))
    assert frames.shape == (7, 112, 64, 3)
    means = [float(f.mean()) for f in frames]
    # Яркость ~ номер × 3 (в «видеодиапазоне» она сжата, поэтому сверяем шаг и порядок).
    steps = [b - a for a, b in zip(means, means[1:])]
    assert all(1.5 <= s <= 4.5 for s in steps), means
    first = float(matte.decode_span(clip, 0, 0, (64, 112))[0].mean())
    assert means[0] - first == pytest.approx(6 * (sum(steps) / len(steps)), abs=2.0)


def test_a_span_past_the_end_returns_what_there_is(clip):
    frames = matte.decode_span(clip, 55, 70, (64, 112))
    assert len(frames) == 5  # в ролике 60 кадров: 55..59


# ------------------------------------------------------------------ замер по маске


BOXES = TitleBoxes(
    big=Rect(70, 200, 400, 100), small=Rect(170, 150, 200, 30), letters=8
)


def person(top: float, *, left: float = 170, right: float = 370, soft: bool = False) -> np.ndarray:
    """Маска «голова и плечи»: человек ниже `top` между `left` и `right` (px кадра)."""
    w, h = matte.MODEL_SIZE
    m = np.zeros((h, w), np.float32)
    sx, sy = w / FRAME[0], h / FRAME[1]
    m[int(top * sy):, int(left * sx):int(right * sx)] = 0.5 if soft else 1.0
    return m


def test_the_coverage_is_the_share_of_the_word_under_the_person():
    # Голова шириной в половину слова заходит на 40 % его высоты: 0.5 × 0.4 = 0.2.
    c = matte.coverage(person(260), BOXES, FRAME)
    assert c.big == pytest.approx(0.2, abs=0.03)
    assert c.big_top == pytest.approx(0.0, abs=0.01)
    assert c.small == 0.0
    assert c.letter == pytest.approx(0.4, abs=0.06)
    assert c.soft < 0.05


def test_shifting_the_title_down_buries_it_deeper():
    rest = matte.coverage(person(260), BOXES, FRAME).big
    lower = matte.coverage(person(260), BOXES, FRAME, dy=30).big
    higher = matte.coverage(person(260), BOXES, FRAME, dy=-30).big
    assert higher < rest < lower


def test_an_unsure_matte_shows_up_as_soft_pixels():
    assert matte.coverage(person(220, soft=True), BOXES, FRAME).soft > 0.3


# ------------------------------------------------------------------ дочерний процесс


def request_for(clip: Path, out: Path, **kw) -> dict:
    base = {
        "video": str(clip), "first": 6, "last": 20, "step": 1, "frame": list(FRAME),
        "big": [70, 200, 400, 100], "small": [170, 150, 200, 30], "letters": 8,
        "shifts": [-20.0, 0.0, 20.0], "out_dir": str(out), "threads": 1,
    }
    base.update(kw)
    return base


def test_a_head_in_the_word_turns_the_trick_on_and_writes_the_masks(clip, tmp_path):
    out = tmp_path / "matte" / "title"
    decision = matte.run_request(
        request_for(clip, out), estimator=lambda frames: np.stack([person(262)] * len(frames))
    )
    assert decision.on and decision.reason is None
    assert (decision.first, decision.last) == (6, 20)
    assert decision.dir == str(out)
    assert 0.08 <= decision.cover <= 0.35

    files = sorted(p.name for p in out.iterdir())
    assert files == [f"{n:05d}.png" for n in range(6, 21)]
    mask = Image.open(out / "00006.png")
    assert mask.mode == "LA" and mask.size == (FRAME[0] // 2, FRAME[1] // 2)
    alpha = np.asarray(mask)[:, :, 1]
    # Альфа — «НЕ человек»: текст остаётся там, где её много.
    assert alpha[10, 135] > 250  # небо над головой
    assert alpha[300, 135] < 5  # голова


def test_the_title_is_moved_to_where_the_head_meets_it(clip, tmp_path):
    # Голова ниже слова: в покое она его не касается, а на 20 px ниже — заходит.
    decision = matte.run_request(
        request_for(clip, tmp_path / "m"),
        estimator=lambda frames: np.stack([person(285)] * len(frames)),
    )
    assert decision.on and decision.dy == 20.0


@pytest.mark.parametrize(
    "estimator, why",
    [
        (lambda frames: np.zeros((len(frames), *matte.MODEL_SIZE[::-1]), np.float32), "не касается"),
        (lambda frames: np.ones((len(frames), *matte.MODEL_SIZE[::-1]), np.float32), "закрывает"),
        (lambda frames: np.stack([person(240, soft=True)] * len(frames)), "маска"),
    ],
)
def test_a_bad_matte_means_the_ordinary_title(clip, tmp_path, estimator, why):
    out = tmp_path / "matte" / "title"
    decision = matte.run_request(request_for(clip, out), estimator=estimator)
    assert not decision.on and why in decision.reason
    assert not out.exists()


def test_a_video_shorter_than_the_card_is_refused(clip, tmp_path):
    decision = matte.run_request(
        request_for(clip, tmp_path / "m", first=50, last=70),
        estimator=lambda frames: np.stack([person(262)] * len(frames)),
    )
    assert not decision.on and "кончилось" in decision.reason


def test_at_sixty_frames_the_model_runs_every_other_frame(clip, tmp_path):
    calls = []

    def estimator(frames):
        calls.append(len(frames))
        return np.stack([person(262)] * len(frames))

    decision = matte.run_request(request_for(clip, tmp_path / "m", step=2), estimator=estimator)
    assert decision.on and calls == [8]  # 15 кадров через один
    assert len(list((tmp_path / "m").iterdir())) == 15  # а маска есть на каждом


# ------------------------------------------------------------------ вход


def edl_with_title(title: str = "Вышел убийца кода", **kw) -> EDL:
    ws = [Word(idx=i, text=t, start=1.5 + i * 0.3, end=1.5 + i * 0.3 + 0.25)
          for i, t in enumerate(["это", "он"])]
    data = dict(
        source_duration=2.0, words=ws, cuts=[], preset="pulse", title=title,
        captions=[CaptionBlock(words=[
            CaptionWord(word_idx=0), CaptionWord(word_idx=1, emphasis=Emphasis.ACCENT)])],
    )
    data.update(kw)
    return EDL(**data)


def track() -> FaceTrack:
    return FaceTrack(fps=FPS, points=[
        TrackPoint(f=f, cx=0.5, cy=0.45, h=0.22, conf=0.9) for f in range(60)
    ])


class Runner:
    def __init__(self, decision: BehindDecision | Exception | None = None):
        self.calls: list[dict] = []
        self.decision = decision

    def __call__(self, request: dict, timeout: float) -> BehindDecision:
        self.calls.append(request)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision or BehindDecision(
            dir=request["out_dir"], first=request["first"], last=request["last"], dy=20.0,
            cover=0.2,
        )


def measure(clip, tmp_path, runner, **kw):
    args = dict(preset=load_preset("pulse"), apply_cuts=False, track=track(), cfg=cfg())
    edl = kw.pop("edl", None) or edl_with_title()
    args.update(kw)
    return matte.measure_behind(edl, clip, tmp_path, runner=runner, **args)


def test_the_request_describes_the_card_of_this_reel(clip, tmp_path):
    runner = Runner()
    decision = measure(clip, tmp_path, runner)
    assert decision.on and decision.dy == 20.0
    request = runner.calls[0]
    assert (request["first"], request["last"]) == (6, 47)
    assert request["frame"] == list(FRAME)
    assert request["letters"] == len("кода")
    assert request["out_dir"] == str(tmp_path / "matte" / "title")
    x, y, w, h = request["big"]
    assert 0 <= x and x + w <= FRAME[0] and 0 < y < FRAME[1] / 2
    assert request["small"] is not None
    # Сдвиги — в пикселях ЭТОГО кадра, в обе стороны от места из темы.
    assert min(request["shifts"]) < 0 < max(request["shifts"])
    assert max(request["shifts"]) == pytest.approx(0.06 * FRAME[1])


@pytest.mark.parametrize(
    "change",
    [
        dict(edl=edl_with_title(title="")),
        dict(edl=edl_with_title(split_until=1.0)),
        dict(track=None),
        dict(cfg=cfg(matte_enabled=False)),
        dict(preset=load_preset("glass")),  # тема без титра
    ],
)
def test_when_there_is_nothing_to_measure_the_model_is_not_started(clip, tmp_path, change):
    runner = Runner()
    assert measure(clip, tmp_path, runner, **change) is None
    assert runner.calls == []


def test_a_theme_can_refuse_the_trick(clip, tmp_path):
    preset = load_preset("pulse").model_copy(deep=True)
    preset.caption.title_card.behind = False
    runner = Runner()
    assert measure(clip, tmp_path, runner, preset=preset) is None and runner.calls == []


def test_a_picture_over_the_card_keeps_the_ordinary_title(clip, tmp_path):
    edl = edl_with_title()
    edl.words.append(Word(idx=2, text="кадр", start=0.3, end=0.6))
    edl.captions.insert(0, CaptionBlock(words=[CaptionWord(word_idx=2)]))
    edl.words.sort(key=lambda w: w.start)
    edl.broll = [Broll(word_idx=2, duration=1.5, query="q", anim="pop_in", anchor="bottom_right")]
    runner = Runner()
    assert measure(clip, tmp_path, runner, edl=edl) is None and runner.calls == []


def test_a_lost_face_keeps_the_ordinary_title(clip, tmp_path):
    lost = track()
    lost.points[20].conf = 0.1
    runner = Runner()
    assert measure(clip, tmp_path, runner, track=lost) is None and runner.calls == []


def test_a_crash_or_a_timeout_never_costs_the_reel(clip, tmp_path):
    (tmp_path / "matte" / "title").mkdir(parents=True)
    (tmp_path / "matte" / "title" / "00006.png").write_bytes(b"old")
    for failure in (RuntimeError("boom"), subprocess.TimeoutExpired("matte", 1)):
        assert measure(clip, tmp_path, Runner(failure)) is None
        assert not (tmp_path / "matte").exists()


def test_a_refusal_leaves_no_masks_behind(clip, tmp_path):
    refusal = BehindDecision(first=6, last=47, reason="голова не касается большого слова")
    decision = measure(clip, tmp_path, Runner(refusal))
    assert decision is not None and not decision.on
    assert not (tmp_path / "matte").exists()


# ------------------------------------------------------------------ модель


def test_the_model_in_the_repository_is_the_one_the_thresholds_were_tuned_on():
    assert matte.MODEL.exists(), "нет файла модели — приём молча выключится"
    assert matte.model_ok()
    assert all(side % 32 == 0 for side in matte.MODEL_SIZE)


@pytest.mark.slow
def test_the_real_model_finds_the_person_in_a_real_frame():
    """Настоящая модель на настоящем кадре: человек — в центре, по углам сверху его нет."""
    sample = Path(__file__).resolve().parents[1] / "samples" / "phase-0-source.mp4"
    if not sample.exists():
        pytest.skip("нет образца")
    frames = matte.decode_span(sample, 150, 151, matte.MODEL_SIZE)
    mattes = matte.refine(matte.make_estimator(threads=2)(frames))
    assert mattes.shape == (2, matte.MODEL_SIZE[1], matte.MODEL_SIZE[0])
    h, w = mattes.shape[1:]
    assert mattes[0, h // 2 - 20: h // 2 + 20, w // 2 - 20: w // 2 + 20].mean() > 0.9
    assert mattes[0, :20, :20].mean() < 0.2
