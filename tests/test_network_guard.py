"""Предохранитель: тест не может выйти в интернет (`conftest._no_network`).

За внешними адресами этого кода стоят платные API. Если двойника забыли, а ключ
в окружении живой, тест обязан упасть, а не потратить деньги.

Адреса в тестах — заведомо не свои: 203.0.113.0/24 зарезервирована под примеры
(RFC 5737) и никуда не ведёт, так что даже без предохранителя пакет не дошёл бы
до чужого сервера.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from tests.conftest import NetworkBlocked, is_local_host

OUTSIDE_IP = "203.0.113.7"


def test_a_name_outside_this_machine_does_not_resolve() -> None:
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("api.openai.com", 443)
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("fal.run", 443), timeout=1)


def test_a_numeric_address_outside_this_machine_does_not_connect() -> None:
    # Адрес цифрами имён не разрешает — его ловит сам `connect`.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        with pytest.raises(NetworkBlocked):
            s.connect((OUTSIDE_IP, 443))
        with pytest.raises(NetworkBlocked):
            s.connect_ex((OUTSIDE_IP, 443))


def test_httpx_refuses_a_real_request() -> None:
    with pytest.raises(NetworkBlocked):
        httpx.get("https://api.openai.com/v1/models", timeout=1)
    with httpx.Client(timeout=1) as client:
        with pytest.raises(NetworkBlocked):
            client.post(f"https://{OUTSIDE_IP}/v1/images", json={})


async def test_async_httpx_refuses_a_real_request() -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        with pytest.raises(NetworkBlocked):
            await client.post("https://api.openai.com/v1/chat/completions", json={})


async def test_asyncio_refuses_a_numeric_address() -> None:
    # На Windows asyncio соединяется через ConnectEx, мимо `socket.connect`.
    with pytest.raises(NetworkBlocked):
        await asyncio.wait_for(asyncio.open_connection(OUTSIDE_IP, 443), timeout=2)


async def test_asyncio_refuses_a_name() -> None:
    with pytest.raises(NetworkBlocked):
        await asyncio.wait_for(asyncio.open_connection("api.openai.com", 443), timeout=2)


def test_a_mock_transport_still_works() -> None:
    # Двойник транспорта в сеть не ходит, и предохранитель ему не мешает.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://api.openai.com/v1/models").json() == {"ok": True}


async def test_this_machine_stays_reachable() -> None:
    # Свой компьютер открыт: на нём база, тестовые серверы и Redis.
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"pong")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read(4) == b"pong"
        writer.close()
        reader, writer = await asyncio.open_connection("localhost", port)
        assert await reader.read(4) == b"pong"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "host, local",
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.8.9.1", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", True),
        (None, True),
        (b"127.0.0.1", True),
        ("api.openai.com", False),
        ("fal.run", False),
        ("localhost.evil.example", False),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
        (OUTSIDE_IP, False),
        ("2001:db8::1", False),
    ],
)
def test_what_counts_as_this_machine(host: object, local: bool) -> None:
    assert is_local_host(host) is local
