"""Заголовок за головой: решение по маске человека (чистая логика).

Титр первых секунд может уйти ЗА голову говорящего: текст рисуется поверх
фона, а человек его перекрывает. Приём хорош ровно в узком коридоре — голова
должна закрыть заметную, но небольшую часть большого слова. Мимо коридора
(слово нечитаемо, голова его не касается, маска сомнительная, человек двигается)
титр рисуется как раньше: приём не может сделать ролик хуже.
"""

from __future__ import annotations

import pytest

from src.core.behind import (
    BIG_MEDIAN,
    Coverage,
    pick_shift,
    span_frames,
    title_boxes,
    verdict,
)
from src.core.preset import load_preset
from src.core.render_plan import PlanBlock, PlanWord, _rect_of_lines, wrap_block

FRAME = (1080, 1920)


def good(big: float = 0.18, **kw) -> Coverage:
    base = dict(big=big, big_top=0.0, letter=min(1.0, big * 2), small=0.0, soft=0.03)
    base.update(kw)
    return Coverage(**base)


# ------------------------------------------------------------------ вердикт


def test_a_head_rising_into_the_word_passes():
    assert verdict([good(0.16), good(0.18), good(0.2)]) is None


@pytest.mark.parametrize(
    "frames, why",
    [
        ([], "кадров"),
        ([good(0.01)] * 3, "не касается"),
        ([good(0.6, letter=0.9)] * 3, "закрывает"),
        ([good(0.18, big_top=0.3)] * 3, "верх"),
        ([good(0.18, letter=0.8)] * 3, "букв"),
        ([good(0.18, small=0.2)] * 3, "подпись"),
        ([good(0.18, soft=0.4)] * 3, "маска"),
        ([good(0.09), good(0.2), good(0.33)], "двигается"),
    ],
)
def test_everything_outside_the_corridor_is_refused(frames, why):
    reason = verdict(frames)
    assert reason is not None and why in reason


def test_one_bad_frame_is_enough():
    """Маска, сорвавшаяся на одном кадре, — это мигнувшая буква в готовом ролике."""
    assert verdict([good(0.18)] * 10 + [good(0.18, soft=0.5)]) is not None


# ------------------------------------------------------------------ сдвиг


def test_the_shift_closest_to_the_target_cover_wins():
    table = {
        -20.0: [good(0.02)] * 3,  # голова не достаёт
        0.0: [good(0.1)] * 3,
        20.0: [good(0.19)] * 3,
        40.0: [good(0.3)] * 3,
        60.0: [good(0.5, letter=0.9)] * 3,  # слово уже не читается
    }
    assert pick_shift(table) == 20.0


def test_between_equal_covers_the_smaller_shift_wins():
    table = {30.0: [good(0.18)] * 3, -10.0: [good(0.18)] * 3}
    assert pick_shift(table) == -10.0


def test_no_shift_passes_means_no_trick():
    assert pick_shift({0.0: [good(0.01)] * 3, 20.0: [good(0.7, letter=1.0)] * 3}) is None
    assert pick_shift({}) is None


def test_the_corridor_is_what_the_docstring_says():
    assert 0.05 <= BIG_MEDIAN[0] < BIG_MEDIAN[1] <= 0.4


# ------------------------------------------------------------------ кадры


@pytest.mark.parametrize("fps, first, last", [(30, 6, 47), (24, 5, 39), (60, 12, 91)])
def test_the_span_covers_the_card_and_its_tail(fps, first, last):
    """Маска нужна на всех кадрах, где титр виден, включая хвост ухода."""
    preset = load_preset("pulse")
    assert span_frames(0.2, 1.3, fps, preset) == (first, last)


# ------------------------------------------------------------------ коробки


def card_block(title: str = "Вышел убийца монтажёров"):
    ps = load_preset("pulse").scaled(FRAME[1])
    card_ps = ps.caption.title_card.preset(ps)
    parts = title.split()
    words = [
        PlanWord(text=t, start=0.2, end=1.3,
                 emphasis="accent" if i == len(parts) - 1 else "none", word_idx=-1 - i)
        for i, t in enumerate(parts)
    ]
    lines, widths = wrap_block(words, card_ps, FRAME[0])
    rect = _rect_of_lines("top", lines, widths, card_ps, FRAME)
    return PlanBlock(start=0.2, end=1.3, position="top", lines=lines, rect=rect,
                     title_card=True), card_ps


def test_the_big_word_box_is_its_ink_and_sits_in_the_card():
    block, card_ps = card_block()
    boxes = title_boxes(block, card_ps, FRAME)
    big_size = max(w.size for w in block.words)
    # Высота — прописная буква большого слова, ширина — его строка.
    assert boxes.big.height == pytest.approx(0.7 * big_size, rel=0.02)
    assert boxes.big.x >= 0 and boxes.big.x + boxes.big.width <= FRAME[0]
    assert boxes.big.x + boxes.big.width / 2 == pytest.approx(FRAME[0] / 2, abs=0.5)
    assert boxes.letters == len("монтажёров")
    # Подпись — выше большого слова и уже его.
    assert boxes.small is not None
    assert boxes.small.y < boxes.big.y
    assert boxes.small.height < boxes.big.height
    # Чернила лежат около коробки блока (у титра они слегка торчат из неё).
    assert block.rect.y - 0.15 * big_size <= boxes.big.y <= block.rect.y + block.rect.height


def test_a_one_word_title_has_no_caption_line():
    block, card_ps = card_block("Никогда")
    boxes = title_boxes(block, card_ps, FRAME)
    assert boxes.small is None and boxes.letters == len("Никогда")


# ------------------------------------------------------------------ план рендера
#
# Решение приезжает в `build_plan` отдельным входом. Титр встаёт на подобранное
# место и несёт маску; без решения план — тот же, что и раньше.

from src.core.behind import BehindDecision
from src.core.config import Settings
from src.core.edl import EDL, CaptionBlock, CaptionWord, Emphasis, Word
from src.core.render_plan import RenderPlan, build_plan


def plan_cfg() -> Settings:
    return Settings(_env_file=None, bot_token="x", sub_tail=0.15, render_fps=30,
                    render_width=1080, render_height=1920)


def titled_edl(position: str = "auto") -> EDL:
    ws = [Word(idx=i, text=t, start=1.6 + i * 0.3, end=1.6 + i * 0.3 + 0.25)
          for i, t in enumerate(["это", "он"])]
    return EDL(
        source_duration=3.0, words=ws, cuts=[], preset="pulse", title="Вышел убийца кода",
        captions=[CaptionBlock(position=position, words=[
            CaptionWord(word_idx=0), CaptionWord(word_idx=1, emphasis=Emphasis.ACCENT)])],
    )


def plan_with(decision: BehindDecision | None, edl: EDL | None = None) -> RenderPlan:
    return build_plan(edl or titled_edl(), "v.mp4", apply_cuts=False, cfg=plan_cfg(),
                      behind=decision)


def card_of(plan: RenderPlan):
    return next(b for b in plan.blocks if b.title_card)


ON = BehindDecision(dir="/work/matte/title", first=6, last=47, dy=38.0, cover=0.2)


def test_without_a_decision_the_plan_is_what_it_was():
    plain = plan_with(None)
    assert card_of(plain).behind is None
    refused = plan_with(BehindDecision(first=6, last=47, reason="голова не касается слова"))
    assert refused.to_dict() == plain.to_dict()


def test_the_card_moves_to_the_measured_place_and_carries_the_mask():
    plain, shifted = card_of(plan_with(None)), card_of(plan_with(ON))
    assert shifted.behind is not None
    assert (shifted.behind.dir, shifted.behind.first, shifted.behind.last) == (
        "/work/matte/title", 6, 47,
    )
    assert shifted.rect.y == pytest.approx(plain.rect.y + 38)
    assert shifted.rect.height == pytest.approx(plain.rect.height)
    assert shifted.lines == plain.lines
    assert shifted.anchor_path is None and not shifted.panel


def test_the_renderer_gets_the_same_margin_the_rect_was_built_with():
    """Отступ титра рендерер берёт из пресета в плане — он обязан быть сдвинутым."""
    plain, shifted = plan_with(None), plan_with(ON)
    assert (
        shifted.preset["caption"]["title_card"]["margin"]
        == plain.preset["caption"]["title_card"]["margin"] + 38
    )
    assert card_of(shifted).rect.y == pytest.approx(
        shifted.preset["caption"]["title_card"]["margin"]
    )
    # Тема в кеше не тронута: следующий ролик начнёт с её собственного отступа.
    assert plan_with(None).preset == plain.preset


def test_a_decision_for_another_card_is_ignored():
    stale = ON.model_copy(update={"first": 9})
    assert plan_with(stale).to_dict() == plan_with(None).to_dict()


def test_a_shifted_card_that_meets_a_caption_falls_back_to_the_ordinary_one():
    """Сдвиг увёл титр на субтитр — рисуем обычный титр, а не остаёмся без него."""
    edl = titled_edl(position="center")
    edl.words[0].start, edl.words[0].end = 0.5, 0.8
    edl.words[1].start, edl.words[1].end = 0.8, 1.1
    plain = plan_with(None, edl)
    caption = next(b for b in plain.blocks if not b.title_card)
    card = card_of(plain)
    assert not card.rect.overlaps(caption.rect)
    # Сдвиг, который сажает титр прямо на субтитр в центре кадра.
    onto = ON.model_copy(update={"dy": caption.rect.y - card.rect.y})
    pushed = plan_with(onto, edl)
    assert card_of(pushed).behind is None
    assert pushed.to_dict() == plain.to_dict()


def test_a_theme_that_refuses_the_trick_ignores_the_decision():
    preset = load_preset("pulse").model_copy(deep=True)
    preset.caption.title_card.behind = False
    plan = build_plan(titled_edl(), "v.mp4", apply_cuts=False, cfg=plan_cfg(), behind=ON,
                      preset=preset)
    assert card_of(plan).behind is None


def test_the_mask_survives_the_json_round_trip():
    plan = plan_with(ON)
    again = RenderPlan.from_dict(plan.to_dict())
    assert card_of(again).behind == card_of(plan).behind
    data = plan.to_dict()
    for b in data["blocks"]:
        b.pop("behind")
    assert all(b.behind is None for b in RenderPlan.from_dict(data).blocks)


# ------------------------------------------------------------------ рендер


from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "render"


def _masks(folder: Path, first: int, last: int, size=(54, 96)) -> None:
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    for n in range(first, last + 1):
        Image.new("LA", size, (255, 255)).save(folder / f"{n:05d}.png")


def test_the_masks_ride_with_the_render_assets(tmp_path):
    from src.pipeline.render import prepare_assets

    masks = tmp_path / "work" / "matte" / "title"
    _masks(masks, 6, 47)
    plan = plan_with(ON.model_copy(update={"dir": str(masks)}))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    plan.video = str(video)
    assets = tmp_path / "assets"
    prepare_assets(plan, assets, cfg=plan_cfg())
    card = card_of(plan)
    # В плане — имя папки относительно директории джоба, как у видео и шрифтов.
    assert card.behind.dir == "matte-title"
    assert sorted(p.name for p in (assets / "matte-title").iterdir()) == [
        f"{n:05d}.png" for n in range(6, 48)
    ]


def test_masks_already_inside_the_job_folder_are_not_copied(tmp_path):
    from src.pipeline.render import prepare_assets

    assets = tmp_path / "job"
    masks = assets / "matte" / "title"
    _masks(masks, 6, 47)
    plan = plan_with(ON.model_copy(update={"dir": str(masks)}))
    video = assets / "video.mp4"
    video.write_bytes(b"x")
    plan.video = str(video)
    prepare_assets(plan, assets, cfg=plan_cfg())
    assert card_of(plan).behind.dir == "matte/title"
    assert not (assets / "matte-title").exists()


def test_a_missing_mask_takes_the_trick_off_entirely(tmp_path):
    """Титр с маской на половине кадров мигал бы: нет хоть одной — рисуем поверх."""
    from src.pipeline.render import prepare_assets

    masks = tmp_path / "matte" / "title"
    _masks(masks, 6, 47)
    (masks / "00020.png").unlink()
    plan = plan_with(ON.model_copy(update={"dir": str(masks)}))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    plan.video = str(video)
    prepare_assets(plan, tmp_path / "assets", cfg=plan_cfg())
    assert card_of(plan).behind is None


def test_the_renderer_reads_the_masks_the_way_python_writes_them():
    behind = (RENDER / "src" / "layers" / "Behind.tsx").read_text()
    captions = (RENDER / "src" / "layers" / "Captions.tsx").read_text()
    types = (RENDER / "src" / "types.ts").read_text()
    cli = (RENDER / "render.mjs").read_text()

    # Имя файла — пять цифр номера кадра: `matte.write_masks` пишет `{n:05d}.png`.
    assert 'String(n).padStart(5, "0")}.png' in behind
    # Маска кадра — по альфе, на весь кадр, зажатая в диапазон.
    assert "Math.min(behind.last, Math.max(behind.first, frame))" in behind
    # Без камеры маска ровно на весь кадр (`scale` = 1 → 100% и 0px), с камерой —
    # та же трансформа, что у видео: крупнее в `scale` раз и сдвинута от точки
    # (ox, oy), которая стоит на месте.
    assert "const scale = camera?.scale ?? 1" in behind
    assert "(scale * 100).toFixed(3)" in behind and '"0px 0px"' in behind
    assert "camera.ox * (1 - scale)" in behind and "camera.oy * (1 - scale)" in behind
    assert "maskSize: size" in behind and "maskPosition: position" in behind
    assert "WebkitMaskImage" in behind
    # Маски готовы ДО первого кадра: загружены, декодированы, поставлены как data:.
    assert "delayRender" in behind and "image.decode()" in behind and "readAsDataURL" in behind
    # И рендер отпускается после перерисовки, а не из промиса.
    assert "if (masks !== null) continueRender(handle)" in behind
    assert "...behindStyle(block, frame, masks, camera)" in captions
    assert "useBehindMasks(blocks)" in captions
    assert captions.index("useBehindMasks(blocks)") < captions.index("if (visible.length === 0) return null")
    assert "behind?: {" in types
    assert "dir: withPrefix(prefix, b.behind.dir)" in cli


@pytest.mark.slow
def test_the_mask_cuts_the_title_on_every_frame(tmp_path):
    """Настоящий рендер: «человек» с движущимся краем режет титр ровно по своему краю.

    Край маски сдвигается на 8 px за кадр. Если маска опоздает на кадр, не
    успеет загрузиться или ляжет со сдвигом — в полосе у края окажется текст
    (или его не окажется рядом), и тест это увидит. Проверяется каждый кадр, где
    титр стоит в полную силу.
    """
    import shutil
    import subprocess

    import numpy as np
    from PIL import Image

    from src.pipeline.render import render_remotion

    if shutil.which("node") is None or not (RENDER / "node_modules").exists():
        pytest.skip("нужен node и render/node_modules")

    W, H, FPS = 540, 960, 30
    clip = tmp_path / "grey.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=0x404040:s={W}x{H}:r={FPS}:d=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    cfg = Settings(_env_file=None, bot_token="x", sub_tail=0.15, render_fps=FPS,
                   render_width=W, render_height=H)
    edl = titled_edl()
    edl.source_duration = 2.0
    edl.words[0].start, edl.words[0].end = 1.7, 1.8
    edl.words[1].start, edl.words[1].end = 1.8, 1.9

    first, last = 6, 47
    edge = lambda n: 150 + 8 * (n - first)  # noqa: E731 — левый край «человека», px кадра
    masks = tmp_path / "matte" / "title"
    masks.mkdir(parents=True)
    for n in range(first, last + 1):
        alpha = np.full((H // 2, W // 2), 255, np.uint8)
        alpha[:, edge(n) // 2:] = 0  # правее края — человек, текста там нет
        mask = Image.new("LA", (W // 2, H // 2), (255, 255))
        mask.putalpha(Image.fromarray(alpha))
        mask.save(masks / f"{n:05d}.png")

    decision = BehindDecision(dir=str(masks), first=first, last=last, dy=0.0, cover=0.2)
    plan = build_plan(edl, str(clip), fps=FPS, width=W, height=H, apply_cuts=False, cfg=cfg,
                      behind=decision)
    card = card_of(plan)
    assert card.behind is not None
    out = render_remotion(plan, tmp_path / "out.mp4", assets_dir=tmp_path / "assets",
                          job_key="behind-test", cfg=cfg)

    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-f", "rawvideo",
         "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W)
    y0, y1 = int(card.rect.y), int(card.rect.y + card.rect.height)
    checked = 0
    # Только кадры, где титр стоит в полную силу: в начале он проявляется, а
    # перед концом уже гаснет (у pulse блок уходит ДО своего конца).
    for n in range(first + 12, round(card.end * FPS) - 6):
        band = frames[n][y0:y1]
        x = edge(n)
        # Край маски мягкий (растяжение вдвое) — отступаем от него на 4 px.
        hidden, shown = band[:, x + 4:], band[:, : x - 4]
        assert hidden.max() < 110, f"кадр {n}: текст виден ЗА краем маски (x={x})"
        assert shown.max() > 200, f"кадр {n}: текста нет ПЕРЕД краем маски (x={x})"
        # Текст доходит до самого края: маска не опоздала и не убежала на кадр.
        near = band[:, x - 12: x - 4]
        assert near.max() > 150, f"кадр {n}: у края маски пусто — маска не того кадра"
        checked += 1
    assert checked >= 12


# ------------------------------------------------------------------ конвейер задачи


def test_the_matte_stage_feeds_every_plan_the_job_builds():
    """Три пути собирают план: новая задача, пересборка по листу и смена темы.

    Маска считается на каждом — после кропа, по кадру, который увидит рендерер, —
    и её решение уезжает в `build_plan`. Забытый путь значил бы, что титр уходит
    за голову в новой задаче и перестаёт при смене темы.
    """
    job = (REPO / "src" / "pipeline" / "job.py").read_text()
    tasks = (REPO / "src" / "worker" / "tasks.py").read_text()
    assert job.count("_measure_behind,") + job.count("_measure_behind(") >= 3
    assert job.count("behind=behind,") == 2
    assert "_measure_behind," in tasks and "behind=behind," in tasks
    # В новой задаче и при смене темы замер идёт в потоке: на цикле событий
    # висит пульс задачи, а модель считает секунды.
    assert "await asyncio.to_thread(\n            _measure_behind," in job
    assert "await asyncio.to_thread(\n            _measure_behind," in tasks


def test_the_fallback_renderer_never_starts_the_model(monkeypatch, tmp_path):
    from src.pipeline import job, matte

    called = []
    monkeypatch.setattr(matte, "measure_behind", lambda *a, **k: called.append(1))
    out = job._measure_behind(
        titled_edl(), tmp_path / "v.mp4", tmp_path, preset=load_preset("pulse"),
        cut_pauses=False, backend="ass", cfg=plan_cfg(),
    )
    assert out is None and called == []


def test_the_stage_hands_the_track_of_this_job_to_the_matte(monkeypatch, tmp_path):
    from src.core.track import FaceTrack, TrackPoint
    from src.pipeline import job, matte

    track = FaceTrack(fps=30, points=[TrackPoint(f=0, cx=0.5, cy=0.4, h=0.2, conf=0.9)])
    (tmp_path / "track.json").write_text(track.model_dump_json(), encoding="utf-8")
    seen = {}

    def fake(edl, video, work, **kw):
        seen.update(kw, video=video, work=work)
        return ON

    monkeypatch.setattr(matte, "measure_behind", fake)
    out = job._measure_behind(
        titled_edl(), tmp_path / "v.mp4", tmp_path, preset=load_preset("pulse"),
        cut_pauses=True, backend="remotion", cfg=plan_cfg(),
    )
    assert out is ON
    assert seen["track"].points[0].cy == 0.4 and seen["apply_cuts"] is True
    assert seen["work"] == tmp_path


def test_a_broken_matte_stage_never_costs_the_reel(monkeypatch, tmp_path):
    from src.pipeline import job, matte

    def boom(*a, **k):
        raise RuntimeError("нет onnxruntime")

    monkeypatch.setattr(matte, "measure_behind", boom)
    out = job._measure_behind(
        titled_edl(), tmp_path / "v.mp4", tmp_path, preset=load_preset("pulse"),
        cut_pauses=False, backend="remotion", cfg=plan_cfg(),
    )
    assert out is None
