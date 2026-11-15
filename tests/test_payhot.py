"""PayHot: клиент API и способы оплаты из настроек. Сеть не трогается.

Проверено на кассе 15 ноября 2026: карта в RUB маршрута не имеет, карта в EUR,
СБП и криптовалюта в RUB — имеют. Поэтому карта считается в евро по расчёту
самого PayHot (`payment-estimates`), а остальное — прямо в рублях пакета.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.core.config import Settings
from src.payments import payhot
from src.payments.payhot import PayHotClient, PayHotError, methods

KEY = "phk_v2_test.secret"


def client_with(handler) -> PayHotClient:
    return PayHotClient(
        base_url="https://app.pay.hot", api_key=KEY,
        transport=httpx.MockTransport(handler),
    )


async def test_a_payment_is_created_with_the_key_and_idempotency_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["idem"] = request.headers.get("idempotency-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "payment": {"id": "pay-1", "status": "requires_payment_method",
                        "amount_minor": "109900", "currency": "RUB"},
            "checkout": {"url": "https://go.pay.hot/pay/x#t=secret"},
        })

    async with client_with(handler) as c:
        got = await c.create_payment(
            order_ref="ord-1", amount_minor=109900, currency="RUB", method="crypto",
            customer_id="tg-42", success_url="https://t.me/ClipLy_app",
        )
    assert got == payhot.Created(payment_id="pay-1", checkout_url="https://go.pay.hot/pay/x#t=secret")
    assert seen["url"] == "https://app.pay.hot/api/v2/payments"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["idem"] == "ord-1"
    assert seen["body"] == {
        "merchant_order_reference": "ord-1", "amount_minor": "109900", "currency": "RUB",
        "payment_method": "crypto", "customer": {"id": "tg-42"},
        "success_return_url": "https://t.me/ClipLy_app", "failure_return_url": "https://t.me/ClipLy_app",
    }


async def test_the_status_is_read_back():
    def handler(request):
        assert request.url.path == "/api/v2/payments/pay-1"
        return httpx.Response(200, json={"payment": {
            "id": "pay-1", "status": "succeeded", "amount_minor": "109900", "currency": "RUB",
            "merchant_order_reference": "ord-1",
        }})

    async with client_with(handler) as c:
        got = await c.get_payment("pay-1")
    assert got == payhot.Status(status="succeeded", amount_minor=109900, currency="RUB", order_ref="ord-1")


async def test_the_euro_amount_for_a_ruble_price_comes_from_payhot():
    def handler(request):
        assert request.url.path == "/api/v2/payment-estimates"
        q = dict(request.url.params)
        assert q == {"currency": "EUR", "target_currency": "RUB", "target_amount_minor": "109900",
                     "payment_method": "card"}
        return httpx.Response(200, json={"status": "estimated", "currency": "EUR", "amount_minor": "1309"})

    async with client_with(handler) as c:
        assert await c.euro_for_rubles(109900) == 1309


async def test_the_wallet_estimate_asks_for_the_wallet_method():
    def handler(request):
        assert dict(request.url.params)["payment_method"] == "applepay"
        return httpx.Response(200, json={"status": "estimated", "currency": "EUR", "amount_minor": "1434"})

    async with client_with(handler) as c:
        assert await c.euro_for_rubles(109900, method="applepay") == 1434


@pytest.mark.parametrize("status, body, code", [
    (422, {"error": {"code": "payment_route_unavailable", "message": "no route"}}, "payment_route_unavailable"),
    (401, {"error": {"code": "authentication_failed"}}, "authentication_failed"),
    (503, {}, "http_503"),
])
async def test_a_refusal_carries_its_code(status, body, code):
    async with client_with(lambda r: httpx.Response(status, json=body)) as c:
        with pytest.raises(PayHotError) as e:
            await c.get_payment("pay-1")
    assert e.value.code == code


async def test_a_network_failure_is_a_payhot_error():
    def handler(request):
        raise httpx.ConnectError("нет сети", request=request)

    async with client_with(handler) as c:
        with pytest.raises(PayHotError) as e:
            await c.get_payment("pay-1")
    assert e.value.code == "network"


def test_methods_come_in_the_owners_order():
    """Порядок владельца: Apple Pay, СБП, Visa/Mastercard (Европа), Google Pay,
    крипта. Кошельки и карта — в EUR (рублёвых маршрутов у кассы нет), СБП и
    крипта — в RUB. Проверено пробными счетами 15–16 ноября 2026."""
    cfg = Settings(_env_file=None, payhot_api_key=KEY)
    got = [(m.key, m.payhot_method, m.currency) for m in methods(cfg)]
    assert got == [
        ("applepay", "applepay", "EUR"), ("sbp", "sbp", "RUB"), ("card", "card", "EUR"),
        ("googlepay", "googlepay", "EUR"), ("crypto", "crypto", "RUB"),
    ]


def test_each_method_has_its_own_switch():
    cfg = Settings(_env_file=None, payhot_api_key=KEY, payhot_applepay_enabled=False,
                   payhot_payment_enabled=False, payhot_googlepay_enabled=False)
    assert [m.key for m in methods(cfg)] == ["card", "crypto"]
    assert methods(cfg.model_copy(update={"payhot_api_key": ""})) == []


def test_a_minimum_hides_a_method_for_a_small_package():
    cfg = Settings(_env_file=None, payhot_api_key=KEY, payhot_crypto_enabled=True,
                   payhot_card_enabled=False, payhot_crypto_min_amount_minor=200000)
    crypto = payhot.method(cfg, "crypto")
    assert crypto.allows_rub(1099) is False
    assert crypto.allows_rub(2799) is True
