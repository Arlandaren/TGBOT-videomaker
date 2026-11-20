"""Провайдер OpenAI: что ушло бы в API и как читается ответ. Сам API не трогается.

Провайдер написан, но не подключён, и ни один тест здесь к OpenAI не ходит:
сеть подменена `httpx.MockTransport`, ключ — заведомо фальшивый `sk-test`.
Проверяется диалект — адрес, заголовки, тело запроса — и разбор ответа: это
ровно то, что при включении либо сработает с первого раза, либо сожжёт деньги
на отказах.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from typing import get_args
from unittest import mock

import httpx
import pytest

from src.core.config import Settings
from src.llm import client as client_mod
from src.llm import images as imagegen
from src.llm import images_openai, openai_provider, providers
from src.llm.client import LLMBadOutput, LLMBudgetExceeded, LLMClient, LLMUnavailable
from src.llm.images_openai import generate_openai
from src.llm.openai_provider import (
    OPENAI_BASE_URL,
    OPENAI_MODELS,
    OPENAI_PRICES,
    OpenAIProvider,
    image_mime,
    strict_ready,
)
from src.llm.providers import ProviderError
from src.llm.schemas import CLEAN_SCHEMA, DIRECT_SCHEMA

KEY = "sk-test"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 4000
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 400

# Схема, которую строгий режим OpenAI принимает: все поля обязательные, у каждого
# объекта `additionalProperties: false`, ключевые слова — из поддерживаемых.
STRICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "note"],
    "properties": {
        "note": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["word_idx", "kind"],
                "properties": {
                    "word_idx": {"type": "integer", "minimum": 0},
                    "kind": {"type": "string", "enum": ["chip", "sticker"]},
                },
            },
        },
    },
}


def ok(text: str = '{"fixes": []}', *, finish: str = "stop", usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": None},
                "finish_reason": finish,
            }
        ],
        "usage": usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 5},
    }


class Wire:
    """Подменный сервер: запоминает запросы и отвечает заготовленным."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        got = self.answer(request) if callable(self.answer) else self.answer
        return got if isinstance(got, httpx.Response) else httpx.Response(200, json=got)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def wired(provider: OpenAIProvider, answer) -> Wire:
    """Клиента строит сам провайдер — свой адрес, свои заголовки. Меняется только провод.

    Иначе тест проверял бы заголовки, которые сам же и написал. Провод
    подставляется в момент создания клиента: настоящий транспорт httpx при
    рождении грузит сертификаты, а это полсекунды на каждый тест.
    """
    wire = Wire(answer)
    real = httpx.AsyncClient

    def over_the_wire(**kwargs):
        return real(transport=httpx.MockTransport(wire), **kwargs)

    with mock.patch.object(httpx, "AsyncClient", over_the_wire):
        provider._ensure()
    return wire


async def ask(provider: OpenAIProvider, **over):
    kw = dict(model="gpt-6-luna", system="СИСТЕМА", user="ЗАДАЧА", schema=CLEAN_SCHEMA)
    kw.update(over)
    try:
        return await provider.complete(**kw)
    finally:
        await provider.aclose()


# ------------------------------------------------------------------ выключен


def test_openai_is_connected_by_the_owners_decision():
    """Владелец 20.11.2026: «fal не будем юзать, только GPT».

    Тест на месте прежнего «написан, но не подключён» — снят осознанно.
    `LLM_PROVIDER=openai` даёт настоящий `OpenAIProvider` (не DeepSeek-
    совместимый класс из реестра) с ключом `OPENAI_API_KEY`; режиссёр — terra,
    простые шаги — luna; `IMAGE_PROVIDER=openai` рисует через Images API.
    Значение по умолчанию у обоих по-прежнему не OpenAI: включает .env.
    """
    cfg = Settings(_env_file=None, bot_token="x", llm_provider="openai",
                   image_provider="openai", openai_api_key="sk-test")
    assert cfg.llm_api_key == "sk-test" and cfg.llm_ready
    assert cfg.llm_base_url == "https://api.openai.com/v1"
    assert cfg.model_for_tier("smart") == "gpt-6.1-terra"
    assert cfg.model_for_tier("vision") == "gpt-6.1-terra"
    assert cfg.model_for_tier("cheap") == "gpt-6-luna"
    made = providers.make_provider("openai", cfg.llm_api_key, cfg.llm_base_url)
    assert isinstance(made, OpenAIProvider) and made.name == "openai"
    assert imagegen.image_key(cfg) == "sk-test" and imagegen.ready(cfg)
    assert imagegen.image_model(cfg) == "gpt-image-2.5-flare"
    for model in ("gpt-6-luna", "gpt-6.1-terra"):
        assert providers.price_for(model) != providers.FALLBACK_PRICE
    default = Settings(_env_file=None, bot_token="x")
    assert default.llm_provider != "openai" and default.image_provider != "openai"


async def test_the_image_provider_openai_goes_through_generate_openai():
    """`images.generate` с IMAGE_PROVIDER=openai шлёт вертикаль черновым качеством."""
    cfg = Settings(_env_file=None, bot_token="x", image_provider="openai",
                   openai_api_key="sk-test")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()
    seen: list[httpx.Request] = []

    def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": png}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        got = await imagegen.generate(client, "кот на диване", cfg=cfg)
    assert got and got.startswith(b"\x89PNG")
    body = json.loads(seen[0].content)
    assert seen[0].url.path.endswith("/images/generations")
    assert body["size"] == "1024x1536" and body["quality"] == "low"
    assert body["model"] == "gpt-image-2.5-flare" and body["background"] == "opaque"


def test_import_and_construction_make_no_request(monkeypatch):
    """Импорт и создание провайдера не открывают ни одного соединения.

    Модули загружаются заново под пробным именем, а httpx при этом считает
    созданные клиенты: ленивый клиент обязан появиться только на первом вызове.
    """
    built: list[str] = []
    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def spy_async(*args, **kwargs):
        built.append("AsyncClient")
        return real_async(*args, **kwargs)

    def spy_sync(*args, **kwargs):
        built.append("Client")
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy_async)
    monkeypatch.setattr(httpx, "Client", spy_sync)

    fresh = {}
    for module in (openai_provider, images_openai):
        path = Path(module.__file__)
        spec = importlib.util.spec_from_file_location(f"{path.stem}_probe", path)
        fresh[path.stem] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh[path.stem])

    provider = fresh["openai_provider"].OpenAIProvider(KEY)
    assert built == []
    assert provider._http is None
    assert provider.base_url == "https://api.openai.com/v1"


def test_modules_do_not_look_for_a_key_themselves():
    """Ключ приходит аргументом. Сам модуль не читает ни окружение, ни .env, ни конфиг."""
    for module in (openai_provider, images_openai):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for needle in ("environ", "getenv", "dotenv", "src.core.config"):
            assert needle not in source, f"{Path(module.__file__).name}: {needle}"


def test_constructor_fits_the_registry_factory_call():
    """`make_provider` зовёт класс как (ключ, адрес, таймаут, thinking=...)."""
    p = OpenAIProvider(KEY, "https://example.test/v1/", 30.0, thinking=True)
    assert (p.api_key, p.base_url) == (KEY, "https://example.test/v1")
    assert (p.timeout, p.thinking) == (30.0, True)
    assert p.supports_schema and p.supports_images and not p.supports_audio


# ------------------------------------------------------------------ запрос: текст


async def test_text_request_goes_to_chat_completions_with_a_bearer_key():
    p = OpenAIProvider(KEY)
    wire = wired(p, ok())
    res = await ask(p)

    req = wire.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert req.headers["content-type"] == "application/json"
    assert res.text == '{"fixes": []}'
    assert len(wire.requests) == 1


async def test_body_with_a_schema_is_exactly_the_openai_dialect():
    p = OpenAIProvider(KEY)
    wire = wired(p, ok())
    await ask(p, max_tokens=4000)

    assert wire.bodies[0] == {
        "model": "gpt-6-luna",
        # системное сообщение первое и отдельное — на совпадении префикса стоит кеш
        "messages": [
            {"role": "developer", "content": "СИСТЕМА"},
            {"role": "user", "content": "ЗАДАЧА"},
        ],
        "max_completion_tokens": 4000,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": False, "schema": CLEAN_SCHEMA},
        },
        "reasoning_effort": "none",
    }


async def test_body_without_a_schema_asks_for_plain_text():
    p = OpenAIProvider(KEY)
    wire = wired(p, ok("просто текст"))
    res = await ask(p, schema=None)

    assert wire.bodies[0] == {
        "model": "gpt-6-luna",
        "messages": [
            {"role": "developer", "content": "СИСТЕМА"},
            {"role": "user", "content": "ЗАДАЧА"},
        ],
        "max_completion_tokens": 16000,
        "reasoning_effort": "none",
    }
    assert res.text == "просто текст"


@pytest.mark.parametrize("thinking", [False, True])
async def test_deepseek_keys_never_reach_openai(thinking):
    """`thinking`, `max_tokens` и `temperature` — диалект DeepSeek, OpenAI их не ждёт."""
    p = OpenAIProvider(KEY, thinking=thinking)
    wire = wired(p, ok())
    await ask(p, effort="high", images=[JPEG])

    body = wire.bodies[0]
    for alien in ("thinking", "max_tokens", "temperature", "reasoning"):
        assert alien not in body
    assert body["response_format"]["type"] == "json_schema"


# ------------------------------------------------------------------ запрос: картинки


async def test_images_go_as_data_urls_before_the_text():
    p = OpenAIProvider(KEY)
    wire = wired(p, ok())
    await ask(p, images=[JPEG, PNG])

    parts = wire.bodies[0]["messages"][1]["content"]
    assert [part["type"] for part in parts] == ["image_url", "image_url", "text"]
    first, second = parts[0]["image_url"]["url"], parts[1]["image_url"]["url"]
    assert first.startswith("data:image/jpeg;base64,")
    assert second.startswith("data:image/png;base64,")
    assert base64.b64decode(first.split(",", 1)[1]) == JPEG
    assert base64.b64decode(second.split(",", 1)[1]) == PNG
    assert parts[-1] == {"type": "text", "text": "ЗАДАЧА"}
    # системное сообщение остаётся строкой: картинки — часть задачи, а не правил
    assert wire.bodies[0]["messages"][0] == {"role": "developer", "content": "СИСТЕМА"}


def test_image_type_is_read_from_the_bytes():
    assert image_mime(JPEG) == "image/jpeg"
    assert image_mime(PNG) == "image/png"
    assert image_mime(b"GIF89a....") == "image/gif"
    assert image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_mime(b"\x00\x01 unknown") == "image/jpeg"


# ------------------------------------------------------------------ схема: строгий режим


def test_strict_mode_is_only_for_schemas_that_qualify():
    assert strict_ready(STRICT_SCHEMA) is True
    # `panel` у блока необязателен намеренно — строгий режим такого не допускает.
    assert strict_ready(DIRECT_SCHEMA) is False
    # `maxLength` в списке поддерживаемых ключевых слов не значится.
    assert strict_ready(CLEAN_SCHEMA) is False
    assert strict_ready({"type": "array", "items": {"type": "integer"}}) is False
    assert strict_ready({"type": "object"}) is False
    assert strict_ready(None) is False
    open_object = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    assert strict_ready(open_object) is False
    # узел без типа — «что угодно»: строгий режим такого не описывает
    untyped = {
        "type": "object", "additionalProperties": False, "required": ["a"], "properties": {"a": {}},
    }
    assert strict_ready(untyped) is False


async def test_qualifying_schema_is_enforced_strictly():
    p = OpenAIProvider(KEY)
    wire = wired(p, ok('{"items": [], "note": null}'))
    await ask(p, schema=STRICT_SCHEMA)

    assert wire.bodies[0]["response_format"]["json_schema"] == {
        "name": "answer",
        "strict": True,
        "schema": STRICT_SCHEMA,
    }


async def test_strict_mode_can_be_switched_off_by_hand():
    p = OpenAIProvider(KEY, strict=False)
    wire = wired(p, ok())
    await ask(p, schema=STRICT_SCHEMA)
    assert wire.bodies[0]["response_format"]["json_schema"]["strict"] is False


async def test_schema_rejected_by_strict_mode_is_resent_loosely_and_remembered():
    """`invalid_json_schema` — не конец вызова: нестрогий режим ту же схему примет."""

    def answer(request: httpx.Request):
        body = json.loads(request.content)
        if body["response_format"]["json_schema"]["strict"]:
            return httpx.Response(
                400,
                json={"error": {"message": "Invalid schema", "code": "invalid_json_schema"}},
            )
        return ok('{"items": [], "note": null}')

    p = OpenAIProvider(KEY)
    wire = wired(p, answer)
    first = await p.complete(model="gpt-6-luna", system="s", user="u", schema=STRICT_SCHEMA)
    await p.complete(model="gpt-6-luna", system="s", user="u", schema=STRICT_SCHEMA)
    await p.aclose()

    assert first.text == '{"items": [], "note": null}'
    strict_flags = [b["response_format"]["json_schema"]["strict"] for b in wire.bodies]
    assert strict_flags == [True, False, False], "второй раз спорный режим не пробуется"
    # усилие размышлений при этом не тронуто: виновата была схема
    assert {b["reasoning_effort"] for b in wire.bodies} == {"none"}


# ------------------------------------------------------------------ размышления


async def test_thinking_off_asks_for_no_reasoning():
    """Размышления считаются в `max_completion_tokens` и платятся как выход."""
    p = OpenAIProvider(KEY)
    wire = wired(p, ok())
    await ask(p, effort="high")
    assert wire.bodies[0]["reasoning_effort"] == "none"


@pytest.mark.parametrize(
    "effort,sent",
    [("low", "low"), ("medium", "medium"), ("high", "high"), (None, None), ("?", None)],
)
async def test_thinking_on_passes_the_effort_through(effort, sent):
    p = OpenAIProvider(KEY, thinking=True)
    wire = wired(p, ok())
    await ask(p, effort=effort)
    assert wire.bodies[0].get("reasoning_effort") == sent


async def test_model_that_cannot_stop_thinking_gets_the_least_it_accepts_and_is_remembered():
    """gpt-6.2-sol на `none` отвечает 400 — модель не должна из-за этого пропасть."""

    def answer(request: httpx.Request):
        effort = json.loads(request.content)["reasoning_effort"]
        if effort in ("none", "minimal"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": f"Unsupported value: 'reasoning_effort' does not support "
                        f"'{effort}' with this model.",
                        "type": "invalid_request_error",
                        "param": "reasoning_effort",
                    }
                },
            )
        return ok()

    p = OpenAIProvider(KEY)
    wire = wired(p, answer)
    await p.complete(model="gpt-6.2-sol", system="s", user="u", schema=STRICT_SCHEMA)
    await p.complete(model="gpt-6.2-sol", system="s", user="u", schema=STRICT_SCHEMA)
    # у другой модели своя память: ей снова предлагается «не думать»
    await p.complete(model="gpt-6-luna", system="s", user="u", schema=STRICT_SCHEMA)
    await p.aclose()

    assert [b["reasoning_effort"] for b in wire.bodies] == [
        "none", "minimal", "low",  # первая встреча: лестница
        "low",  # урок запомнен
        "none", "minimal", "low",
    ]
    # схема ни при чём — строгий режим пережил все отказы
    assert all(b["response_format"]["json_schema"]["strict"] for b in wire.bodies)


async def test_unrecognised_400_still_walks_the_effort_ladder_once():
    """Слов отказа документация не приводит — на них одних полагаться нельзя."""

    def answer(request: httpx.Request):
        if json.loads(request.content)["reasoning_effort"] == "none":
            return httpx.Response(400, json={"error": {"message": "Unsupported value."}})
        return ok()

    p = OpenAIProvider(KEY)
    wire = wired(p, answer)
    res = await ask(p, model="gpt-6.2-sol")

    assert res.text == '{"fixes": []}'
    assert [b["reasoning_effort"] for b in wire.bodies] == ["none", "minimal"]


async def test_a_plainly_bad_request_fails_after_the_ladder_and_teaches_nothing():
    """400 по другой причине: лестница конечна, а «урок» из неудачи не выносится."""
    p = OpenAIProvider(KEY)
    wire = wired(p, httpx.Response(400, json={"error": {"message": "context too long"}}))
    with pytest.raises(ProviderError) as e:
        await ask(p)

    assert e.value.status == 400 and e.value.retryable is False
    assert [b["reasoning_effort"] for b in wire.bodies] == ["none", "minimal", "low"]
    assert p._effort_floor == {} and p._not_strict == set()


async def test_with_thinking_on_a_bad_request_is_sent_once():
    p = OpenAIProvider(KEY, thinking=True)
    wire = wired(p, httpx.Response(400, json={"error": {"message": "нет"}}))
    with pytest.raises(ProviderError):
        await ask(p, effort="low")
    assert len(wire.requests) == 1


# ------------------------------------------------------------------ расход


async def test_usage_separates_cached_and_cache_written_tokens():
    """`prompt_tokens` включает и кеш, и запись в него — дважды считать нельзя."""
    usage = {
        "prompt_tokens": 1500,
        "completion_tokens": 120,
        "total_tokens": 1620,
        "prompt_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 200},
        "completion_tokens_details": {"reasoning_tokens": 100},
    }
    p = OpenAIProvider(KEY)
    wired(p, ok(usage=usage))
    res = await ask(p)

    assert res.cache_read_tokens == 1000
    assert res.cache_creation_tokens == 200
    assert res.input_tokens == 300
    assert res.input_tokens + res.cache_read_tokens + res.cache_creation_tokens == 1500
    # размышления уже внутри completion_tokens — не прибавляются второй раз
    assert res.output_tokens == 120
    assert res.billed_usd is None
    assert res.stop_reason == "stop"


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": None, "completion_tokens": None, "prompt_tokens_details": None},
        {"prompt_tokens": "много", "prompt_tokens_details": ["не", "объект"]},
        {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 50}},
    ],
)
async def test_odd_usage_never_crashes_or_goes_negative(usage):
    p = OpenAIProvider(KEY)
    wired(p, ok(usage=usage))
    res = await ask(p)
    assert res.input_tokens >= 0 and res.output_tokens >= 0


def test_proposed_models_and_prices_are_complete():
    """Таблицы — заготовка для шва включения: в них не должно быть дыр."""
    assert set(OPENAI_MODELS) == {"cheap", "smart", "vision"}
    for model in OPENAI_MODELS.values():
        assert model in OPENAI_PRICES, f"{model}: нет цены"
    for model, (miss, hit, out) in OPENAI_PRICES.items():
        assert 0 < hit < miss < out, model


# ------------------------------------------------------------------ ошибки


@pytest.mark.parametrize(
    "status,retryable",
    [(429, True), (500, True), (503, True), (401, False), (403, False), (404, False)],
)
async def test_error_classification_matches_the_other_providers(status, retryable):
    p = OpenAIProvider(KEY)
    wire = wired(p, httpx.Response(status, json={"error": {"message": "нет"}}))
    with pytest.raises(ProviderError) as e:
        await ask(p)

    assert e.value.status == status
    assert e.value.retryable is retryable
    assert len(wire.requests) == 1, "сам провайдер не повторяет — это дело клиента"


async def test_html_body_on_200_is_a_retryable_provider_error():
    page = "<html><title>502 Bad Gateway</title></html>"
    p = OpenAIProvider(KEY)
    wired(p, httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    with pytest.raises(ProviderError) as e:
        await ask(p)
    assert e.value.retryable is True and "502 Bad Gateway" in str(e.value)


async def test_network_failure_is_a_retryable_provider_error():
    def answer(request: httpx.Request):
        raise httpx.ConnectError("соединение сброшено", request=request)

    p = OpenAIProvider(KEY)
    wired(p, answer)
    with pytest.raises(ProviderError) as e:
        await ask(p)
    assert e.value.retryable is True and e.value.status is None


@pytest.mark.parametrize("payload", [{"choices": []}, {"choices": [None]}, {"choices": "?"}, {}])
async def test_answer_without_a_choice_is_empty_text_not_a_crash(payload):
    p = OpenAIProvider(KEY)
    wired(p, payload)
    res = await ask(p)
    assert res.text == "" and res.stop_reason == ""


async def test_refusal_is_reported_as_an_empty_answer_with_its_reason():
    answer = ok()
    answer["choices"][0]["message"] = {"role": "assistant", "content": None, "refusal": "Не могу."}
    p = OpenAIProvider(KEY)
    wired(p, answer)
    res = await ask(p)
    assert res.text == "" and res.stop_reason == "refusal"


# ------------------------------------------------------------------ через клиент


def llm_over(provider: OpenAIProvider) -> LLMClient:
    cfg = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key="test",
        llm_tier_cheap="gpt-6-luna",
        llm_max_retries=1,
    )
    return LLMClient(cfg=cfg, provider=provider)


CLEAN_VARS = {"language": "ru", "words_table": "0 раз"}


async def test_through_the_client_the_schema_is_enforced_not_pasted_into_the_prompt():
    p = OpenAIProvider(KEY)
    wire = wired(p, ok(usage={"prompt_tokens": 900, "completion_tokens": 30}))
    llm = llm_over(p)
    res = await llm.complete("clean_transcript", CLEAN_VARS, CLEAN_SCHEMA, "cheap")
    await llm.aclose()

    body = wire.bodies[0]
    assert res.data == {"fixes": []}
    assert body["model"] == "gpt-6-luna"
    assert body["response_format"]["json_schema"]["schema"] == CLEAN_SCHEMA
    assert "```json" not in body["messages"][1]["content"]
    assert res.usage.provider == "openai"
    assert (res.usage.input_tokens, res.usage.output_tokens) == (900, 30)


async def test_truncated_answer_is_a_budget_error_and_is_paid_for_once():
    """Обрыв по лимиту повтором не лечится — второй запрос сжёг бы деньги зря."""
    p = OpenAIProvider(KEY)
    wire = wired(p, ok('{"fixes": [', finish="length"))
    llm = llm_over(p)
    with pytest.raises(LLMBudgetExceeded):
        await llm.complete("clean_transcript", CLEAN_VARS, CLEAN_SCHEMA, "cheap")
    await llm.aclose()
    assert len(wire.requests) == 1


async def test_refusal_through_the_client_is_bad_output_with_usage_recorded():
    answer = ok(usage={"prompt_tokens": 700, "completion_tokens": 9})
    answer["choices"][0]["message"] = {"role": "assistant", "content": None, "refusal": "Нет."}
    p = OpenAIProvider(KEY)
    wired(p, answer)
    llm = llm_over(p)
    with pytest.raises(LLMBadOutput, match="refusal"):
        await llm.complete("clean_transcript", CLEAN_VARS, CLEAN_SCHEMA, "cheap")
    await llm.aclose()
    assert llm.usages[0].input_tokens == 700, "отказ оплачен — расход обязан быть записан"


async def test_auth_failure_through_the_client_is_unavailable_without_a_retry():
    p = OpenAIProvider(KEY)
    wire = wired(p, httpx.Response(401, json={"error": {"message": "Incorrect API key"}}))
    llm = llm_over(p)
    with pytest.raises(LLMUnavailable):
        await llm.complete("clean_transcript", CLEAN_VARS, CLEAN_SCHEMA, "cheap")
    await llm.aclose()
    assert len(wire.requests) == 1


async def test_overload_through_the_client_is_retried_exactly_once(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", no_sleep)
    p = OpenAIProvider(KEY)
    wire = wired(p, httpx.Response(503, json={"error": {"code": "server_is_overloaded"}}))
    llm = llm_over(p)
    with pytest.raises(LLMUnavailable):
        await llm.complete("clean_transcript", CLEAN_VARS, CLEAN_SCHEMA, "cheap")
    await llm.aclose()
    assert len(wire.requests) == 2


# ------------------------------------------------------------------ картинки


def images_wire(answer) -> tuple[httpx.AsyncClient, Wire]:
    wire = Wire(answer)
    return httpx.AsyncClient(transport=httpx.MockTransport(wire)), wire


def image_answer(data: bytes = PNG) -> dict:
    return {
        "created": 1763400000,
        "background": "transparent",
        "output_format": "png",
        "data": [{"b64_json": base64.standard_b64encode(data).decode("ascii")}],
        "usage": {"input_tokens": 40, "output_tokens": 272, "total_tokens": 312},
    }


async def test_image_request_carries_key_model_and_an_opaque_png():
    client, wire = images_wire(image_answer())
    async with client:
        got = await generate_openai(client, "a ripe red apple", api_key=KEY)

    assert got == PNG
    req = wire.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.openai.com/v1/images/generations"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert wire.bodies[0] == {
        "model": "gpt-image-2.5-flare",
        "prompt": "a ripe red apple",
        "n": 1,
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
        "background": "opaque",
    }


async def test_sticker_is_asked_for_on_a_transparent_background():
    """Прозрачный фон — то, ради чего модуль написан; ему нужен PNG или WebP."""
    client, wire = images_wire(image_answer())
    async with client:
        got = await generate_openai(
            client, "a house", api_key=KEY, transparent=True, size="1024x1536", quality="medium",
            model="gpt-image-2.5-sunburst",
        )

    body = wire.bodies[0]
    assert got == PNG
    assert body["background"] == "transparent" and body["output_format"] == "png"
    assert (body["model"], body["size"], body["quality"]) == (
        "gpt-image-2.5-sunburst", "1024x1536", "medium",
    )
    # GPT Image всегда отвечает base64 и `response_format` не принимает
    assert "response_format" not in body


async def test_image_base_url_is_a_parameter():
    client, wire = images_wire(image_answer())
    async with client:
        await generate_openai(client, "x", api_key=KEY, base_url="https://proxy.test/v1/")
    assert str(wire.requests[0].url) == "https://proxy.test/v1/images/generations"


async def test_image_failure_is_none_and_is_paid_for_once():
    """Картинка необязательна: сбой — `None`, а повтор стоил бы денег."""
    cases = [
        httpx.Response(500, text="boom"),
        httpx.Response(429, json={"error": {"code": "insufficient_quota"}}),
        httpx.Response(400, json={"error": {"code": "moderation_blocked"}}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": [{"url": "https://example.test/i.png"}]}),
        httpx.Response(200, json={"data": [{"b64_json": "это не base64!"}]}),
        httpx.Response(200, json=["не", "объект"]),
        httpx.Response(200, json={"data": {"0": "объект вместо списка"}}),
        httpx.Response(200, json={"data": [None]}),
        httpx.Response(200, json=image_answer(JPEG)),  # просили PNG, пришло другое
    ]
    for case in cases:
        client, wire = images_wire(case)
        async with client:
            assert await generate_openai(client, "x", api_key=KEY) is None
        assert len(wire.requests) == 1


async def test_image_network_failure_is_none():
    def answer(request: httpx.Request):
        raise httpx.ReadTimeout("долго", request=request)

    client, wire = images_wire(answer)
    async with client:
        assert await generate_openai(client, "x", api_key=KEY) is None


async def test_no_key_or_a_broken_size_calls_nothing():
    no_call = ({"api_key": ""}, {"api_key": KEY, "model": ""}, {"api_key": KEY, "size": "vertical"})
    for kw in no_call:
        client, wire = images_wire(image_answer())
        async with client:
            assert await generate_openai(client, "x", **kw) is None
        assert wire.requests == []


def test_image_defaults_point_at_openai_and_a_model_with_transparency():
    assert OPENAI_BASE_URL == "https://api.openai.com/v1"
    assert images_openai.DEFAULT_MODEL.startswith("gpt-image-")
    assert images_openai.OPENAI_IMAGE_TIMEOUT_S >= 120, "сложный промпт считается до двух минут"
