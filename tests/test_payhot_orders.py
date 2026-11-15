"""Заказы PayHot на настоящем Postgres: подтверждение оплаты и ровно одно зачисление.

Деньги приходят ТОЛЬКО по статусу `succeeded` из API PayHot, и только если сумма
и валюта совпали с заказом. Проверку зовут двое — приложение, пока оно открыто,
и фоновый опрос в боте, — поэтому зачисление обязано быть ровно одним при любом
порядке и одновременности вызовов.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from src.db import repo
from src.payments import orders
from src.payments.payhot import PayHotError, Status


@pytest.fixture
async def user(db):
    u = await repo.upsert_user(1001, username="t", first_name="Т", language_code="ru")
    async with repo.session() as s:  # без пробных токенов: считаем только зачисленное
        await s.execute(text("update users set balance = 0 where id = 1001"))
    return u


async def balance_of(uid: int = 1001) -> int:
    return (await repo.get_user(uid)).balance


class FakePayHot:
    def __init__(self, status: str = "succeeded", amount: int = 109900, currency: str = "RUB"):
        self.status, self.amount, self.currency = status, amount, currency
        self.calls = 0

    async def get_payment(self, payment_id: str) -> Status:
        self.calls += 1
        await asyncio.sleep(0)
        return Status(status=self.status, amount_minor=self.amount, currency=self.currency,
                      order_ref="")


async def new_order(payment_id: str = "pay-1", **over):
    args = dict(user_id=1001, tokens=240, rub=1099, method="crypto", currency="RUB",
                amount_minor=109900)
    args.update(over)
    order = await repo.create_payhot_order(**args)
    await repo.set_payhot_payment(order.id, payment_id)
    return await repo.get_payhot_order(order.id)


async def test_a_paid_order_credits_its_tokens_once(user):
    order = await new_order()
    got = await orders.settle(order.id, FakePayHot())
    assert got.credited is True and got.status == "succeeded"
    assert await balance_of() == 240
    again = await orders.settle(order.id, FakePayHot())
    assert again.credited is False and again.status == "succeeded"
    assert await balance_of() == 240, "второе зачисление за одну оплату"


async def test_two_checks_at_once_credit_once(user):
    order = await new_order()
    got = await asyncio.gather(orders.settle(order.id, FakePayHot()), orders.settle(order.id, FakePayHot()))
    assert sorted(g.credited for g in got) == [False, True]
    assert await balance_of() == 240


async def test_the_payment_is_recorded_with_its_rubles(user):
    order = await new_order()
    await orders.settle(order.id, FakePayHot())
    [p] = await repo.user_payments(1001)
    assert (p.tokens, p.stars, p.rub) == (240, 0, 1099)
    assert p.telegram_charge_id == "payhot:pay-1"


@pytest.mark.parametrize("status", ["requires_payment_method", "processing", "authorized"])
async def test_an_unfinished_payment_credits_nothing(user, status):
    order = await new_order()
    got = await orders.settle(order.id, FakePayHot(status=status))
    assert got.credited is False and got.final is False
    assert await balance_of() == 0


@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_a_failed_payment_closes_the_order(user, status):
    order = await new_order()
    got = await orders.settle(order.id, FakePayHot(status=status))
    assert got.final is True and got.credited is False
    assert (await repo.get_payhot_order(order.id)).status == status
    assert await balance_of() == 0


@pytest.mark.parametrize("fake", [FakePayHot(amount=100), FakePayHot(currency="EUR")])
async def test_a_payment_for_another_amount_credits_nothing(user, fake):
    """Оплачено не то, что заказано, — токены не выдаются, заказ на сверку."""
    order = await new_order()
    got = await orders.settle(order.id, fake)
    assert got.credited is False
    assert (await repo.get_payhot_order(order.id)).status == "mismatch"
    assert await balance_of() == 0


async def test_payhot_not_answering_changes_nothing(user):
    class Down:
        async def get_payment(self, pid):
            raise PayHotError("network")

    order = await new_order()
    got = await orders.settle(order.id, Down())
    assert got.credited is False and got.final is False
    assert (await repo.get_payhot_order(order.id)).status == "created"


async def test_a_closed_order_is_not_asked_again(user):
    order = await new_order()
    await orders.settle(order.id, FakePayHot())
    fake = FakePayHot()
    await orders.settle(order.id, fake)
    assert fake.calls == 0


async def test_the_poller_sees_only_open_recent_orders(user):
    from datetime import datetime, timedelta, timezone

    fresh = await new_order("pay-a")
    done = await new_order("pay-b")
    await orders.settle(done.id, FakePayHot())
    old = await new_order("pay-c")
    unpaid = await repo.create_payhot_order(user_id=1001, tokens=240, rub=1099, method="crypto",
                                           currency="RUB", amount_minor=109900)
    assert unpaid.payment_id is None
    async with repo.session() as s:
        await s.execute(text("update payhot_orders set created_at = now() - interval '5 hours' where id = :i"),
                        {"i": old.id})
    ids = {o.id for o in await repo.payhot_orders_to_check(
        since=datetime.now(timezone.utc) - timedelta(hours=3))}
    assert ids == {fresh.id}
