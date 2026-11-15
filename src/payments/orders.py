"""Подтверждение оплаты заказа PayHot и зачисление токенов — ровно один раз.

`settle` зовут двое: приложение, пока человек ждёт на экране, и фоновый опрос в
боте. Порядок и одновременность любые, поэтому опора — уникальный
`payments.telegram_charge_id = "payhot:<payment_id>"` в `repo.credit_payment`:
выигрывает ровно одна вставка, второй вызов получает False и не зачисляет.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from src.db import repo
from src.payments.payhot import PayHotError, Status

log = logging.getLogger(__name__)

PAID = "succeeded"
# Неуспех «на сейчас»: приложению — конец ожидания, но PayHot сам пишет, что
# после позднего подтверждения `failed`/`cancelled` уточняются до succeeded.
# Поэтому заказ остаётся в фоновом опросе (`repo.payhot_orders_to_check`), и
# поздняя оплата всё равно приходит токенами с вестью в чат.
FAILED = ("cancelled", "failed")


class StatusSource(Protocol):
    async def get_payment(self, payment_id: str) -> Status: ...


@dataclass(frozen=True)
class Settled:
    status: str
    credited: bool  # зачислено ИМЕННО этим вызовом
    final: bool
    tokens: int = 0
    user_id: int = 0


async def settle(order_id: uuid.UUID | str, payhot: StatusSource) -> Settled:
    order = await repo.get_payhot_order(order_id)
    if order is None:
        return Settled("missing", False, True)
    if order.credited or order.status in repo.PAYHOT_CLOSED:
        return Settled(order.status, False, True, order.tokens, order.user_id)
    if not order.payment_id:
        return Settled(order.status, False, False, order.tokens, order.user_id)
    try:
        st = await payhot.get_payment(order.payment_id)
    except PayHotError as e:
        log.warning("payhot: заказ %s — статус не получен: %s", order.id, e)
        return Settled(order.status, False, False, order.tokens, order.user_id)

    if st.status == PAID:
        foreign = bool(st.order_ref) and st.order_ref != str(order.id)
        if foreign or st.amount_minor != order.amount_minor or st.currency != order.currency:
            # Оплачено не то, что заказано. Токены не выдаём: это либо подмена
            # суммы, либо ошибка, которую должен разобрать человек.
            log.error("payhot: заказ %s оплачен на %s %s вместо %s %s — на сверку",
                      order.id, st.amount_minor, st.currency, order.amount_minor, order.currency)
            await repo.set_payhot_status(order.id, "mismatch")
            return Settled("mismatch", False, True, order.tokens, order.user_id)
        credited = await repo.credit_payment(
            order.user_id,
            charge_id=f"payhot:{order.payment_id}",
            stars=0,
            tokens=order.tokens,
            rub=order.rub,
            payload=f"payhot:{order.method}:tokens:{order.tokens}",
        )
        await repo.set_payhot_status(order.id, PAID, credited=True)
        if credited:
            log.info("payhot: заказ %s оплачен — +%d токенов пользователю %s",
                     order.id, order.tokens, order.user_id)
        return Settled(PAID, credited, True, order.tokens, order.user_id)

    if st.status in FAILED:
        await repo.set_payhot_status(order.id, st.status)
        return Settled(st.status, False, True, order.tokens, order.user_id)
    if st.status != order.status:
        await repo.set_payhot_status(order.id, st.status)
    return Settled(st.status, False, False, order.tokens, order.user_id)
