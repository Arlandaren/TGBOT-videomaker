"""Графика в сборке ролика: GPT для расстановки, потолок цены, лист → план.

Ни один тест не ходит в OpenAI: провод подменён `httpx.MockTransport`, ключ —
заведомо фальшивый `sk-test`, а сеть из pytest и так закрыта (`conftest`).
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import httpx
import pytest

from src.core.camera import filmed_plan
from src.core.config import Settings
from src.core.edl import EDL
from src.core.overlay_models import Overlay, Visuals
from src.core.preset import load_preset
from src.llm.openai_provider import OpenAIProvider
from src.pipeline import visuals as visuals_mod
from src.pipeline.visuals import plan_visuals, visuals_llm, worst_cost
from tests.test_visuals_plan import good_answer, story


def cfg(**kw: Any) -> Settings:
    base = dict(bot_token="x", openai_api_key="sk-test", visuals_enabled=True, visuals_llm=True)
    return Settings(_env_file=None, **{**base, **kw})


def wired(client, answer) -> list[httpx.Request]:
    """Провод клиента подменён: запросы копятся в списке, ответ заготовлен."""
    seen: list[httpx.Request] = []

    def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer)

    real = httpx.AsyncClient
    with mock.patch.object(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(wire), **kw)
    ):
        client._provider._ensure()
    return seen


def chat(payload: dict, *, prompt: int = 3000, completion: int = 400) -> dict:
    """Ответ Chat Completions с листом графики внутри."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-6-luna",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
            }
        ],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


# ------------------------------------------------------------------ клиент


def test_without_a_key_there_is_no_model_and_no_spending():
    assert visuals_llm(cfg(openai_api_key="")) is None
    assert visuals_llm(cfg(openai_api_key="   ")) is None
    assert visuals_llm(cfg(visuals_llm=False)) is None
    assert visuals_llm(cfg(visuals_enabled=False)) is None


def test_with_a_key_the_graphics_get_their_own_gpt_client():
    """Режиссёр остаётся на своём провайдере (fal), графика — на OpenAI."""
    settings = cfg(visuals_model="gpt-6-luna")
    client = visuals_llm(settings)
    assert client is not None and client.ready
    assert isinstance(client._provider, OpenAIProvider)
    assert client.cfg.model_for_tier(visuals_mod.VISUALS_TIER) == "gpt-6-luna"
    assert settings.llm_provider == "fal", "основной провайдер не тронут"
    assert settings.model_for_tier("smart") != "gpt-6-luna"


async def test_the_gpt_answer_becomes_the_sheet_and_the_request_is_capped():
    edl = story()
    settings = cfg()
    client = visuals_llm(settings)
    seen = wired(client, chat(good_answer()))
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=client, cfg=settings, report=report)
    await client.aclose()

    assert report["from_llm"] is True and len(seen) == 1
    assert [o.kind for o in got.overlays] == ["decor", "chip", "chip", "sticker"]
    body = json.loads(seen[0].content)
    assert seen[0].url.host == "api.openai.com"
    assert body["model"] == "gpt-6-luna"
    assert body["max_completion_tokens"] == settings.visuals_max_tokens
    assert body["reasoning_effort"] == "none", "размышления оплачиваются как выход"
    assert client.usages[0].cost_usd < 0.002


# ------------------------------------------------------------------ потолок цены


async def test_a_call_that_could_cost_more_than_the_cap_is_never_sent():
    edl = story()
    settings = cfg(visuals_max_cost_usd=0.0001)
    client = visuals_llm(settings)
    seen = wired(client, chat(good_answer()))
    report: dict[str, Any] = {}
    got = await plan_visuals(edl, llm=client, cfg=settings, report=report)
    await client.aclose()

    assert seen == [], "запрос ушёл, хотя худший случай дороже потолка"
    assert report["from_llm"] is False
    assert isinstance(got, Visuals), "ролик получил лист бесплатного расстановщика"


async def test_the_retry_also_fits_under_the_cap():
    """Кривой ответ оплачен; повтор уходит, только если и он влезает в потолок."""
    edl = story()
    settings = cfg()
    client = visuals_llm(settings)
    one = worst_cost(client, visuals_mod.prompt_variables(edl, settings), None, 2500)
    settings = cfg(visuals_max_cost_usd=one * 1.2)
    client = visuals_llm(settings)
    seen = wired(client, chat({"overlays": "не список"}, prompt=50_000, completion=2500))
    await plan_visuals(edl, llm=client, cfg=settings)
    await client.aclose()
    assert len(seen) == 1, "повтор ушёл сверх потолка"


def test_the_expected_price_of_one_reel_is_about_a_tenth_of_a_cent():
    edl = story()
    settings = cfg()
    client = visuals_llm(settings)
    worst = worst_cost(client, visuals_mod.prompt_variables(edl, settings), None, 2500)
    assert worst < settings.visuals_max_cost_usd / 2, worst


# ------------------------------------------------------------------ лист → план


def test_the_sheet_survives_the_edl_round_trip():
    edl = story()
    edl.visuals = Visuals(overlays=[Overlay(kind="chip", word_idx=10, text="9 500 000 ₽")])
    again = EDL.model_validate_json(edl.model_dump_json())
    assert again.visuals == edl.visuals
    assert EDL.model_validate_json(story().model_dump_json()).visuals is None


def _plan(edl: EDL, backend: str = "remotion"):
    return filmed_plan(
        edl, "video.mp4", backend=backend, video_duration=edl.source_duration, fps=30,
        width=1080, height=1920, preset=load_preset("pulse"), apply_cuts=False,
        cfg=Settings(_env_file=None, bot_token="x"),
    )


def test_the_render_plan_carries_the_graphics_only_when_the_sheet_has_them():
    edl = story()
    assert "visuals" not in _plan(edl).meta
    edl.visuals = Visuals(overlays=[Overlay(kind="chip", word_idx=10, text="9 500 000 ₽")])
    made = _plan(edl).meta.get("visuals")
    assert made and made["overlays"][0]["kind"] == "chip"
    # Быстрому пути (ass) графику рисовать некому.
    assert "visuals" not in _plan(edl, backend="ass").meta


def test_the_composition_draws_the_stage_and_the_overlays():
    from pathlib import Path

    reel = (Path(__file__).resolve().parents[1] / "render" / "src" / "compositions" / "Reel.tsx")
    text = reel.read_text(encoding="utf-8")
    assert "<Stage plan={plan}>" in text and "<Overlays plan={plan} />" in text
    assert text.index("<Overlays plan={plan} />") < text.index("<Captions"), "текст поверх графики"


def test_captions_on_the_paper_scene_get_dark_ink():
    """Субтитр темы почти белый; на светлой бумаге он не читался."""
    from src.core.overlay_models import Scene
    from src.core.overlays import LIGHT_INK, luminance

    edl = story()
    edl.visuals = Visuals(scenes=[Scene(layout="paper", word_from=12, word_to=20)])
    plan = _plan(edl)
    assert plan.meta["visuals"]["scenes"], "сцена не встала — тест пуст"
    inked = [b for b in plan.blocks if b.ink]
    assert inked and all(b.bg_luma >= 0.5 for b in inked)
    assert all(b.ink is None for b in plan.blocks if b.bg_luma is None)
    # Контраст: текст ≥ 7, акцент ≥ 3 (крупный кегль) на самой бумаге.
    bg = max(b.bg_luma for b in inked)
    ratio = lambda c: (bg + 0.05) / (luminance(c) + 0.05)  # noqa: E731
    assert ratio(LIGHT_INK["text"]) >= 7 and ratio(LIGHT_INK["accent"]) >= 3
    again = type(plan).from_dict(plan.to_dict())
    assert [b.ink for b in again.blocks] == [b.ink for b in plan.blocks]


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5-nano", "gpt-6.1-terra"])
def test_every_offered_model_has_a_price(model):
    from src.llm.providers import FALLBACK_PRICE, price_for

    assert price_for(model) != FALLBACK_PRICE
