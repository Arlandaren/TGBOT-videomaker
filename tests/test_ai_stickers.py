"""Стикеры, которые дорисовывает модель (`pipeline/visuals.draw_stickers`).

К OpenAI тесты не ходят: провод подменён `httpx.MockTransport`, ключ —
заведомо фальшивый `sk-test`, а сеть из pytest и так закрыта (`conftest`).
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from PIL import Image, ImageDraw

from src.core.config import Settings
from src.core.overlay_models import Overlay, Visuals
from src.core.overlay_rules import sanitize_visuals, validate_visuals
from src.core.sticker_art import SIZE
from src.pipeline import visuals as planner
from src.pipeline.visuals import VISUALS_SCHEMA, draw_stickers, visuals_from_answer
from tests.test_visuals_plan import story


def cfg(tmp_path: Path, **kw: Any) -> Settings:
    base = dict(bot_token="x", openai_api_key="sk-test", work_dir=str(tmp_path))
    return Settings(_env_file=None, **{**base, **kw})


def picture() -> str:
    """Картинка «от модели»: цветной круг на прозрачном фоне, base64 PNG."""
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((40, 60, 216, 200), fill=(230, 60, 60, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class Wire:
    def __init__(self, answer: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answer is not None:
            return self.answer
        return httpx.Response(200, json={"data": [{"b64_json": picture()}]})


@pytest.fixture
def wire():
    """Любой `httpx.AsyncClient` в тесте идёт в подменный провод."""
    made = Wire()
    real = httpx.AsyncClient
    with mock.patch.object(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(made), **kw)
    ):
        yield made


def wish(word: int, subject: str, **kw: Any) -> Overlay:
    return Overlay(kind="sticker", word_idx=word, sticker_prompt=subject, **kw)


# ------------------------------------------------------------------ лист


def test_the_model_may_describe_a_sticker_the_set_lacks():
    overlay = VISUALS_SCHEMA["properties"]["overlays"]["items"]["properties"]
    assert "sticker_prompt" in overlay
    got = visuals_from_answer(
        {
            "overlays": [
                {"kind": "sticker", "word_idx": 3, "sticker": "telegram",
                 "sticker_prompt": "paper plane"},
                {"kind": "sticker", "word_idx": 9, "sticker": "house",
                 "sticker_prompt": "big house"},
            ],
            "scenes": [],
        }
    )
    # Чужой ключ снят, описание осталось; у ключа из набора описание не читается.
    assert (got.overlays[0].sticker, got.overlays[0].sticker_prompt) == (None, "paper plane")
    assert (got.overlays[1].sticker, got.overlays[1].sticker_prompt) == ("house", None)


def test_a_described_sticker_survives_the_rules():
    edl = story()
    sheet = Visuals(overlays=[wish(10, "  red sports car "), Overlay(kind="sticker", word_idx=20)])
    clean = sanitize_visuals(sheet, edl)
    assert [(o.word_idx, o.sticker_prompt) for o in clean.overlays] == [(10, "red sports car")]
    assert validate_visuals(edl, clean) == []


# ------------------------------------------------------------------ рисование


async def test_a_described_sticker_is_drawn_outlined_and_cached(tmp_path, wire):
    settings = cfg(tmp_path)
    sheet = Visuals(overlays=[wish(10, "red sports car")])
    got = await draw_stickers(sheet, settings)

    assert len(wire.requests) == 1
    body = json.loads(wire.requests[0].content)
    assert wire.requests[0].url.path.endswith("/images/generations")
    assert body["background"] == "transparent" and body["quality"] == "low"
    assert body["size"] == "1024x1024" and "red sports car" in body["prompt"]

    asset = Path(got.overlays[0].asset)
    assert asset.is_file() and asset.parent == tmp_path / "sticker-cache"
    img = Image.open(asset)
    assert img.mode == "RGBA" and max(img.size) == SIZE, "обведён тем же кодом, что набор"
    # Обводка белая: край непрозрачной области — белый.
    assert img.getpixel((img.width // 2, 8))[:3] == (255, 255, 255)

    # Второй ролик с тем же предметом: запроса нет, файл тот же.
    again = await draw_stickers(Visuals(overlays=[wish(4, "Red Sports Car")]), settings)
    assert len(wire.requests) == 1 and again.overlays[0].asset == str(asset)


async def test_only_a_few_stickers_are_drawn_per_reel(tmp_path, wire):
    settings = cfg(tmp_path, visuals_ai_stickers_max=2)
    sheet = Visuals(
        overlays=[
            wish(4, "paper plane"),
            wish(12, "stack of books"),
            wish(20, "golden key", emoji="🔑"),
            wish(28, "old radio"),
            Overlay(kind="sticker", word_idx=36, sticker="house"),
        ]
    )
    got = await draw_stickers(sheet, settings)
    assert len(wire.requests) == 2, "каждая картинка стоит денег"
    kept = [(o.word_idx, bool(o.asset), o.emoji) for o in got.overlays]
    # Третий остался глифом (у него есть эмодзи), четвёртый снят, набор не тронут.
    assert kept == [(4, True, None), (12, True, None), (20, False, "🔑"), (36, False, None)]


@pytest.mark.parametrize("over", [{"openai_api_key": ""}, {"visuals_ai_stickers_max": 0}])
async def test_without_a_key_or_a_budget_nothing_is_drawn(tmp_path, wire, over):
    sheet = Visuals(overlays=[wish(4, "paper plane"), wish(12, "golden key", emoji="🔑")])
    got = await draw_stickers(sheet, cfg(tmp_path, **over))
    assert wire.requests == []
    assert [(o.word_idx, o.asset) for o in got.overlays] == [(12, None)]


async def test_a_refused_picture_costs_one_request_and_drops_the_sticker(tmp_path):
    refused = Wire(httpx.Response(400, json={"error": {"code": "moderation_blocked"}}))
    real = httpx.AsyncClient
    with mock.patch.object(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(refused), **kw)
    ):
        got = await draw_stickers(Visuals(overlays=[wish(4, "paper plane")]), cfg(tmp_path))
    assert len(refused.requests) == 1 and got.overlays == []


async def test_a_sheet_without_wishes_is_returned_untouched(tmp_path, wire):
    sheet = Visuals(overlays=[Overlay(kind="sticker", word_idx=4, sticker="house")])
    assert await draw_stickers(sheet, cfg(tmp_path)) is sheet
    assert wire.requests == []


def test_the_prompt_asks_for_an_object_not_a_logo():
    text = planner.STICKER_IMAGE_PROMPT.lower()
    for word in ("no text", "no logo", "no people", "transparent background"):
        assert word in text
