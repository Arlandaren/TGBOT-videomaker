"""PayHot: создание платежа, чтение статуса и способы оплаты из настроек.

Подтверждение оплаты — ТОЛЬКО статус `succeeded`, прочитанный у PayHot по API
(`GET /api/v2/payments/{id}`). Возврат человека из браузера и ответ клиента
ничего не подтверждают. Вебхуков нет: адрес Mini App — временный туннель, а
PayHot шлёт уведомления на один постоянный адрес из кабинета. Поэтому статус
опрашивается: из приложения, пока оно открыто, и фоном из бота
(`webapp.server._payhot_poller`).

Документация: https://docs.pay.hot (OpenAPI — /openapi.json).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from src.core.config import Settings

TIMEOUT_S = 20.0


class PayHotError(Exception):
    """PayHot отказал или не ответил. `code` — его код ошибки, `http_NNN` или `network`."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class Created:
    payment_id: str
    checkout_url: str


@dataclass(frozen=True)
class Status:
    status: str
    amount_minor: int
    currency: str
    order_ref: str


@dataclass(frozen=True)
class Method:
    """Способ оплаты в «Тарифах». `key` — наш код, `payhot_method` — код PayHot."""

    key: str
    payhot_method: str
    currency: str  # в чём выставляется счёт: RUB или EUR
    min_amount_minor: int = 0  # в валюте счёта; 0 — без порога

    def allows_rub(self, rub: int) -> bool:
        """Можно ли этим способом пакет за `rub` рублей. Порог карты — в евро, его
        видно только после расчёта PayHot, поэтому здесь он не проверяется."""
        if self.currency != "RUB" or not self.min_amount_minor:
            return True
        return rub * 100 >= self.min_amount_minor


def methods(cfg: Settings) -> list[Method]:
    """Включённые способы PayHot в порядке показа. Без ключа — ни одного."""
    if not cfg.payhot_api_key.strip():
        return []
    out: list[Method] = []
    if cfg.payhot_payment_enabled:
        out.append(Method("sbp", "sbp", "RUB"))
    if cfg.payhot_card_enabled:
        out.append(Method("card", "card", "EUR", cfg.payhot_card_min_amount_minor))
    if cfg.payhot_crypto_enabled:
        out.append(Method("crypto", "crypto", "RUB", cfg.payhot_crypto_min_amount_minor))
    return out


def method(cfg: Settings, key: str) -> Method | None:
    return next((m for m in methods(cfg) if m.key == key), None)


class PayHotClient:
    def __init__(self, *, base_url: str, api_key: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_S,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, cfg: Settings) -> "PayHotClient":
        return cls(base_url=cfg.payhot_base_url, api_key=cfg.payhot_api_key)

    async def __aenter__(self) -> "PayHotClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise PayHotError("network", str(e)) from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict) and err.get("code"):
                raise PayHotError(str(err["code"]), str(err.get("message") or ""))
            raise PayHotError(f"http_{resp.status_code}")
        return data if isinstance(data, dict) else {}

    async def create_payment(
        self, *, order_ref: str, amount_minor: int, currency: str, method: str,
        customer_id: str, success_url: str,
    ) -> Created:
        """Счёт PayHot. `order_ref` — наш номер заказа и он же ключ идемпотентности:
        повтор запроса после обрыва связи не создаёт второй платёж."""
        body = {
            "merchant_order_reference": order_ref,
            "amount_minor": str(int(amount_minor)),
            "currency": currency,
            "payment_method": method,
            "customer": {"id": customer_id},
            "success_return_url": success_url,
            "failure_return_url": success_url,
        }
        data = await self._call("POST", "/api/v2/payments", json=body,
                                headers={"Idempotency-Key": order_ref})
        payment = data.get("payment") or {}
        url = (data.get("checkout") or {}).get("url")
        if not payment.get("id") or not url:
            raise PayHotError("bad_response", "нет id платежа или ссылки оплаты")
        return Created(payment_id=str(payment["id"]), checkout_url=str(url))

    async def get_payment(self, payment_id: str) -> Status:
        data = await self._call("GET", f"/api/v2/payments/{payment_id}")
        p = data.get("payment") or {}
        try:
            return Status(
                status=str(p["status"]), amount_minor=int(p["amount_minor"]),
                currency=str(p["currency"]), order_ref=str(p.get("merchant_order_reference") or ""),
            )
        except (KeyError, ValueError) as e:
            raise PayHotError("bad_response", f"статус без полей: {e}") from e

    async def euro_for_rubles(self, rub_minor: int) -> int:
        """Сумма счёта в евроцентах, при которой зачисление — `rub_minor` копеек.

        Курс и комиссию считает сам PayHot: свой расчёт по курсу разошёлся бы с
        тем, что он зачислит.
        """
        data = await self._call("GET", "/api/v2/payment-estimates", params={
            "currency": "EUR", "target_currency": "RUB",
            "target_amount_minor": str(int(rub_minor)), "payment_method": "card",
        })
        try:
            return int(data["amount_minor"])
        except (KeyError, ValueError) as e:
            raise PayHotError("bad_response", "расчёт без суммы") from e
