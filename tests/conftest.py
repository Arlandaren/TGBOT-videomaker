"""Общие двойники для тестов LLM."""

from __future__ import annotations

import os
from typing import Any, Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.db import repo
from src.db.models import Base
from src.llm.providers import Provider, ProviderResult


class FakeProvider(Provider):
    """Провайдер-двойник: отдаёт заранее заготовленные ответы или кидает ошибки.

    Сеть не трогаем — проверяется наша обвязка, а не чужой API.
    """

    def __init__(
        self,
        script: Sequence[Any],
        *,
        supports_schema: bool = True,
        supports_images: bool = True,
        name: str = "fake",
    ) -> None:
        super().__init__(api_key="test", base_url="http://fake", timeout=5.0)
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.name = name
        self.supports_schema = supports_schema
        self.supports_images = supports_images
        self.closed = False

    async def complete(self, **kwargs: Any) -> ProviderResult:  # type: ignore[override]
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("двойник исчерпан: вызовов больше, чем заготовлено ответов")
        nxt = self.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    async def aclose(self) -> None:
        self.closed = True


def result_of(payload: Any, **usage: Any) -> ProviderResult:
    import json

    return ProviderResult(
        text=json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload,
        input_tokens=usage.get("input_tokens", 100),
        output_tokens=usage.get("output_tokens", 50),
        cache_read_tokens=usage.get("cache_read_tokens", 0),
        cache_creation_tokens=usage.get("cache_creation_tokens", 0),
        stop_reason=usage.get("stop_reason", "stop"),
    )


# --------------------------------------------------- настоящий Postgres
#
# Фикстура общая, а не в одном файле: база нужна всему, что держится на
# транзакции и условии в WHERE, — деньгам, переходам статуса, памяти выбора
# пользователя. Такое двойником не изобразить.

SCHEMA = f"reelbot_test_{os.getpid()}"
DSN = os.environ.get(
    "TEST_POSTGRES_DSN", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres"
)
WHY = (
    f"нужен Postgres по {DSN} — локально поднимается docker-compose, в CI это служба "
    "postgres; адрес переопределяется переменной TEST_POSTGRES_DSN"
)
# В CI пропуск этих тестов недопустим: гейт выкладки из пропусков не сторожит
# ничего, а здесь на кону деньги. Переменную ставит сам workflow, и если базы
# там не окажется, выкладка упадёт, а не позеленеет.
REQUIRED = bool(os.environ.get("TEST_POSTGRES_REQUIRED"))


def _no_database(e: BaseException) -> None:
    (pytest.fail if REQUIRED else pytest.skip)(f"{WHY} ({type(e).__name__}: {e})")


@pytest.fixture
async def db():
    """Своя схема на каждый тест: ни чужих данных, ни следов между тестами.

    Движок создаётся ВНУТРИ теста, а не в session-фикстуре: асинхронный движок
    привязан к тому loop, в котором открыл соединение, и переезд между loop'ами
    даёт «another operation is in progress» вместо понятной ошибки.
    """
    admin = create_async_engine(DSN, poolclass=NullPool)
    try:
        async with admin.begin() as c:
            await c.execute(text(f"drop schema if exists {SCHEMA} cascade"))
            await c.execute(text(f"create schema {SCHEMA}"))
    except Exception as e:  # noqa: BLE001 — базы нет: пропуск локально, провал в CI
        await admin.dispose()
        _no_database(e)

    engine = create_async_engine(
        DSN, connect_args={"server_settings": {"search_path": SCHEMA}}
    )
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    repo._engine = engine
    repo._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # Пробный ролик новичку здесь выключен: денежные тесты считают баланс от
    # нуля, и подарок при создании пользователя сдвинул бы каждое число. Сама
    # проба проверяется отдельно и включает её явно.
    trial_was = repo.default_settings.trial_videos
    repo.default_settings.trial_videos = 0
    try:
        yield
    finally:
        repo.default_settings.trial_videos = trial_was
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f"drop schema if exists {SCHEMA} cascade"))
        await admin.dispose()
        repo._engine = None
        repo._sessionmaker = None


# --------------------------------------------------- сеть в тестах закрыта
#
# Тест не имеет права сходить в интернет: за половиной адресов, которые знает
# этот код, стоят платные API (LLM, генерация картинок, платежи). Двойники и
# `MockTransport` это и так обеспечивают — пока кто-нибудь не забудет подставить
# двойника при живом ключе в окружении. Тогда тест молча потратит деньги и
# останется зелёным. Предохранитель делает такой вызов невозможным: он падает
# до того, как из машины уйдёт хоть один пакет.
#
# Закрыто на трёх уровнях, потому что одного не хватает:
#   - разрешение имён (`socket.getaddrinfo`) — через него идёт почти всё;
#   - само соединение (`socket.connect`) — адрес цифрами имён не разрешает;
#   - `IocpProactor.connect` — на Windows asyncio соединяется через `ConnectEx`
#     мимо `socket.connect`, и без этой заплатки адрес цифрами проходил бы;
#   - настоящие транспорты httpx — чтобы ошибка называла адрес, а не сокет.
# Свой компьютер открыт: на нём Postgres, тестовые серверы aiohttp и Redis.
#
# Тесты с меткой `slow` не закрыты: они грузят модель распознавания, а её
# библиотека сверяется с хабом даже при полном кеше. LLM и картинки они не
# вызывают — там `llm=None` или двойник.

import asyncio  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import httpx  # noqa: E402


class NetworkBlocked(OSError):
    """Тест попытался выйти в сеть. Подставь двойника или `httpx.MockTransport`."""


def _dsn_host() -> str:
    try:
        return (urlsplit(DSN).hostname or "").lower()
    except ValueError:
        return ""


_LOCAL_NAMES = {"", "localhost", _dsn_host()}


def is_local_host(host: Any) -> bool:
    """Свой ли это компьютер: имя `localhost` или адрес обратной петли."""
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "ignore")
    name = str(host).strip().strip("[]").lower()
    if name in _LOCAL_NAMES or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _refuse(host: Any) -> None:
    if not is_local_host(host):
        raise NetworkBlocked(
            f"тест пошёл в сеть ({host!r}): настоящие запросы из тестов запрещены — "
            f"подставь двойника или httpx.MockTransport"
        )


def _address_host(address: Any) -> Any:
    # Кортеж — это (хост, порт, ...) у AF_INET и AF_INET6. Строка или байты —
    # путь к сокету AF_UNIX: это всегда свой компьютер.
    return address[0] if isinstance(address, tuple) and address else None


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("slow") is not None:
        return

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_send = httpx.HTTPTransport.handle_request
    real_asend = httpx.AsyncHTTPTransport.handle_async_request

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        _refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self: socket.socket, address: Any) -> Any:
        _refuse(_address_host(address))
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        _refuse(_address_host(address))
        return real_connect_ex(self, address)

    def send(self: httpx.HTTPTransport, req: httpx.Request) -> httpx.Response:
        _refuse(req.url.host)
        return real_send(self, req)

    async def asend(self: httpx.AsyncHTTPTransport, req: httpx.Request) -> httpx.Response:
        _refuse(req.url.host)
        return await real_asend(self, req)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", asend)

    if sys.platform == "win32":
        from asyncio import windows_events

        real_iocp_connect = windows_events.IocpProactor.connect

        def iocp_connect(self: Any, conn: Any, address: Any) -> Any:
            _refuse(_address_host(address))
            return real_iocp_connect(self, conn, address)

        monkeypatch.setattr(windows_events.IocpProactor, "connect", iocp_connect)
    # Обращение к модулю держит импорт живым для линтера: asyncio нужен здесь
    # только ради ветки Windows выше.
    assert asyncio is not None

