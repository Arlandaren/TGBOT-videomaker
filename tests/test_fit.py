"""Длинный ролик должен уйти в чат: Telegram берёт от бота файл до 50 МБ."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramEntityTooLarge

from src.bot.handlers import video as video_mod
from src.core.config import Settings
from src.pipeline import fit
from src.pipeline import render as render_mod

CLOUD = fit.CLOUD_SEND_MAX_MB * fit.MB


def cfg(**over) -> SimpleNamespace:
    return SimpleNamespace(**{"result_max_mb": 0.0, "bot_api_url": "", **over})


def planned_bytes(b: fit.Budget, duration: float) -> float:
    return b.total_kbps * 1000 / 8 * duration


# ---------------------------------------------------------------- расчёт


def test_the_cloud_limit_is_fifty_megabytes_and_a_local_server_lifts_it():
    assert fit.send_limit_bytes(cfg()) == 50_000_000
    assert fit.send_limit_bytes(cfg(bot_api_url="http://localhost:8081")) == 2_000_000_000
    assert fit.send_limit_bytes(cfg(result_max_mb=8)) == 8_000_000


def test_the_setting_exists_and_is_automatic_by_default():
    assert Settings.model_fields["result_max_mb"].default == 0.0


@pytest.mark.parametrize("duration", [20, 28, 52, 75, 90, 120, 150, 180, 300, 600])
def test_a_reel_of_any_length_is_planned_under_the_limit(duration):
    b = fit.budget(duration, CLOUD)
    assert planned_bytes(b, duration) <= CLOUD * fit.MARGIN + 1000


def test_a_short_reel_keeps_its_sound_and_gets_no_tight_cap():
    """52 с при crf 22 — это ~5000 кбит/с: потолок должен быть заметно выше."""
    b = fit.budget(52, CLOUD)
    assert b.audio_kbps is None
    assert b.video_kbps > 6000


def test_three_minutes_get_a_real_cap_and_lighter_sound():
    b = fit.budget(180, CLOUD)
    assert b.audio_kbps == fit.AUDIO_TIGHT_KBPS
    assert 1800 < b.video_kbps < 2000


def test_the_bitrate_never_goes_to_zero():
    assert fit.budget(100_000, CLOUD).video_kbps == fit.VIDEO_FLOOR_KBPS


def test_render_gets_no_cap_when_it_cannot_matter():
    # Совсем короткий ролик и свой сервер Bot API: кодек работает как раньше.
    assert fit.render_cap(15, cfg()) is None
    assert fit.render_cap(180, cfg(bot_api_url="http://localhost:8081")) is None
    assert fit.render_cap(180, cfg()) == fit.budget(180, CLOUD)


# ---------------------------------------------------------------- рендер


def _plan(duration: float) -> SimpleNamespace:
    return SimpleNamespace(duration=duration, meta={}, to_dict=lambda: {"duration": duration})


def _payload(duration: float, settings) -> dict:
    plan = _plan(duration)
    req = render_mod.RenderRequest(
        plan=plan, out=Path("out.mp4"), assets_dir=Path("."), job_key="j",
        **render_mod._size_cap(plan, settings),
    )
    return json.loads(render_mod._request_payload(req))


def test_the_render_request_carries_the_cap_for_a_long_reel():
    payload = _payload(180, cfg())
    assert payload["maxVideoKbps"] == fit.budget(180, CLOUD).video_kbps
    assert payload["audioKbps"] == fit.AUDIO_TIGHT_KBPS


def test_the_render_request_of_a_mid_reel_caps_video_only():
    payload = _payload(60, cfg())
    assert payload["maxVideoKbps"] > 5000
    assert "audioKbps" not in payload


def test_the_render_request_is_unchanged_without_a_limit():
    payload = _payload(180, cfg(bot_api_url="http://localhost:8081"))
    assert "maxVideoKbps" not in payload and "audioKbps" not in payload


def test_the_render_script_passes_the_cap_to_the_encoder():
    """Python шлёт числа, а в ffmpeg их кладёт `render.mjs` — проверяем обе стороны."""
    script = (render_mod.RENDER_DIR / "render.mjs").read_text(encoding="utf-8")
    for name in ("req.maxVideoKbps", "encodingMaxRate", "encodingBufferSize",
                 "req.audioKbps", "audioBitrate"):
        assert name in script, name


# ---------------------------------------------------------------- отправка

pytest_anyio = pytest.mark.anyio


@pytest_anyio
async def test_a_file_under_the_limit_is_sent_as_is(tmp_path):
    f = tmp_path / "r.mp4"
    f.write_bytes(b"x" * 1000)
    assert await fit.for_chat(f, 2000) == f


@pytest_anyio
async def test_a_missing_file_is_not_this_modules_problem(tmp_path):
    f = tmp_path / "nope.mp4"
    assert await fit.for_chat(f, 2000) == f


def _make_video(path: Path, seconds: int = 6) -> None:
    """Шум вместо картинки: он не сжимается, файл выходит заведомо жирным."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-nostdin",
         "-f", "lavfi", "-i", f"testsrc2=s=320x568:r=30:d={seconds}",
         "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
         "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, timeout=120,
    )


@pytest_anyio
async def test_a_file_over_the_limit_is_squeezed_under_it(tmp_path):
    src = tmp_path / "r.mp4"
    _make_video(src)
    size = src.stat().st_size
    limit = 400_000
    assert size > limit, "фикстура должна быть больше предела"

    fitted = await fit.for_chat(src, limit)

    assert fitted != src and fitted.name == "r.fit.mp4"
    assert 0 < fitted.stat().st_size <= limit
    assert src.stat().st_size == size, "оригинал не трогаем"
    # Копия — целый ролик той же длины, со звуком.
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
         "-of", "json", str(fitted)], capture_output=True, text=True, check=True, timeout=60,
    )
    meta = json.loads(out.stdout)
    assert {s["codec_type"] for s in meta["streams"]} == {"video", "audio"}
    assert abs(float(meta["format"]["duration"]) - 6.0) < 0.5


@pytest_anyio
async def test_a_failed_squeeze_says_so_and_leaves_no_copy(tmp_path, monkeypatch):
    src = tmp_path / "r.mp4"
    src.write_bytes(b"not a video" * 1000)
    with pytest.raises(fit.TooBigToSend):
        await fit.for_chat(src, 100)
    assert not (tmp_path / "r.fit.mp4").exists()


@pytest_anyio
async def test_a_squeeze_that_missed_the_limit_is_not_sent(tmp_path, monkeypatch):
    src = tmp_path / "r.mp4"
    src.write_bytes(b"x" * 5000)

    async def fake_ffmpeg(args, *, timeout=None):
        Path(args[-1]).write_bytes(b"y" * 3000)
        return ""

    monkeypatch.setattr(fit, "affmpeg", fake_ffmpeg)
    monkeypatch.setattr(
        "src.pipeline.probe.probe", lambda p: SimpleNamespace(duration=10.0)
    )
    with pytest.raises(fit.TooBigToSend):
        await fit.for_chat(src, 2000)
    assert not (tmp_path / "r.fit.mp4").exists()


# ---------------------------------------------------------------- бот


@dataclass
class FakeJob:
    id: uuid.UUID
    preset: str = "glass"
    source_duration: float | None = 30.0
    result_duration: float | None = 20.0
    result_key: str | None = "results/r.mp4"


class FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[Path] = []
        self.calls = 0

    async def send_video(self, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        self.sent.append(Path(kw["video"].path))


@pytest.fixture
def no_frame(monkeypatch):
    """Размеры и обложка здесь не проверяются — не гоняем ради них ffmpeg."""

    async def nothing(*a, **kw):
        return {}

    async def no_thumb(*a, **kw):
        return None

    monkeypatch.setattr(video_mod, "video_dims", nothing)
    monkeypatch.setattr(video_mod, "video_thumbnail", no_thumb)


@pytest_anyio
async def test_the_bot_sends_the_squeezed_copy_and_cleans_it_up(tmp_path, monkeypatch, no_frame):
    src = tmp_path / "deliver.mp4"
    src.write_bytes(b"x" * 5000)
    copy = tmp_path / "deliver.fit.mp4"

    async def fake_fit(path, limit):
        assert limit == 50_000_000  # облачный Bot API
        copy.write_bytes(b"y" * 100)
        return copy

    monkeypatch.setattr(video_mod.fit, "for_chat", fake_fit)
    bot = FakeBot()
    await video_mod._send_video(bot, 1, FakeJob(id=uuid.uuid4()), src, "glass")

    assert bot.sent == [copy]
    assert not copy.exists(), "копию убираем сами"
    assert src.exists(), "оригинал убирает вызывающий"


@pytest_anyio
async def test_the_copy_is_cleaned_up_when_sending_fails(tmp_path, monkeypatch, no_frame):
    src = tmp_path / "deliver.mp4"
    src.write_bytes(b"x")
    copy = tmp_path / "deliver.fit.mp4"

    async def fake_fit(path, limit):
        copy.write_bytes(b"y")
        return copy

    monkeypatch.setattr(video_mod.fit, "for_chat", fake_fit)
    with pytest.raises(ValueError):
        await video_mod._send_video(
            FakeBot(error=ValueError("не тот чат")), 1, FakeJob(id=uuid.uuid4()), src, "glass"
        )
    assert not copy.exists()


@pytest_anyio
async def test_too_large_is_not_retried(tmp_path, monkeypatch, no_frame):
    """В aiogram это сетевая ошибка, но четыре заливки подряд ничего не изменят."""

    async def no_sleep(sec):
        raise AssertionError("ждать повтора незачем")

    monkeypatch.setattr(video_mod.asyncio, "sleep", no_sleep)
    bot = FakeBot(error=TelegramEntityTooLarge(method=None, message="Request Entity Too Large"))
    with pytest.raises(TelegramEntityTooLarge):
        await video_mod._send_video(bot, 1, FakeJob(id=uuid.uuid4()), tmp_path / "r.mp4", "glass")
    assert bot.calls == 1
