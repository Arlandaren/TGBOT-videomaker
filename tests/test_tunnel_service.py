"""Туннель Mini App отдельной службой: адрес переживает выкладку.

Быстрый туннель Cloudflare выдаёт новый адрес на каждый запуск cloudflared.
Пока туннель жил внутри бота, каждая выкладка (перезапуск бота) меняла адрес, и
открытое у пользователя приложение умирало с «адрес не найден». Служба держит
cloudflared сама и пишет адрес в файл; бот адрес только читает.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.webapp import tunnel as mod

REPO = Path(__file__).resolve().parents[1]
URL = "https://abc-def.trycloudflare.com"
OTHER = "https://new-one.trycloudflare.com"


def fake_cloudflared(tmp_path: Path, lives_s: str) -> Path:
    fake = tmp_path / "cloudflared"
    fake.write_text(f"#!/bin/sh\necho 'INF {URL}' >&2\nexec sleep {lives_s}\n")
    fake.chmod(0o755)
    return fake


async def _until(check, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "не дождался"
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------ служба


async def test_the_service_publishes_the_address_while_the_tunnel_lives(tmp_path):
    url_file = tmp_path / "tunnel.url"
    url_file.write_text("https://stale-old.trycloudflare.com\n")  # от прошлого запуска

    task = asyncio.create_task(
        mod.serve(fake_cloudflared(tmp_path, "0.6"), "http://127.0.0.1:1", url_file)
    )
    await _until(lambda: mod.read_url(url_file) == URL)

    # cloudflared умер сам: адрес мёртв, файл убран, код возврата просит перезапуск.
    assert await asyncio.wait_for(task, 5) == 1
    assert not url_file.exists()


async def test_a_stopped_service_leaves_no_address_behind(tmp_path):
    url_file = tmp_path / "tunnel.url"
    task = asyncio.create_task(
        mod.serve(fake_cloudflared(tmp_path, "30"), "http://127.0.0.1:1", url_file)
    )
    await _until(lambda: mod.read_url(url_file) == URL)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not url_file.exists()


# ------------------------------------------------------------------ чтение ботом


def test_only_a_whole_tunnel_address_counts(tmp_path):
    f = tmp_path / "tunnel.url"
    assert mod.read_url(f) == ""  # файла нет
    f.write_text("")
    assert mod.read_url(f) == ""
    f.write_text("https://abc-def.trycloudfl")  # недописанный
    assert mod.read_url(f) == ""
    f.write_text("https://evil.example.com\n")
    assert mod.read_url(f) == ""
    f.write_text(URL + "\n")
    assert mod.read_url(f) == URL


async def test_the_bot_waits_for_the_address_to_appear(tmp_path):
    f = tmp_path / "tunnel.url"

    async def appear():
        await asyncio.sleep(0.1)
        f.write_text(URL + "\n")

    writer = asyncio.create_task(appear())
    assert await mod.wait_for_url(f, timeout_s=5, poll_s=0.02) == URL
    await writer


async def test_no_address_in_time_is_an_error_not_a_hang(tmp_path):
    with pytest.raises(mod.TunnelError):
        await mod.wait_for_url(tmp_path / "tunnel.url", timeout_s=0.1, poll_s=0.02)


async def test_a_new_address_restarts_the_bot(monkeypatch, tmp_path):
    """Служба туннеля перезапустилась — у всех кнопок бота мёртвый адрес."""
    restarts = []
    monkeypatch.setattr(mod, "restart_self", lambda: restarts.append(1))
    f = tmp_path / "tunnel.url"
    f.write_text(URL + "\n")

    watch = asyncio.create_task(mod.watch_url(f, URL, poll_s=0.02))
    await asyncio.sleep(0.1)
    assert restarts == []  # тот же адрес

    f.unlink()  # служба перезапускается: файла какое-то время нет
    await asyncio.sleep(0.1)
    assert restarts == []

    f.write_text(OTHER + "\n")
    await asyncio.wait_for(watch, 5)
    assert restarts == [1]


# ------------------------------------------------------------------ бот


class _Bot:
    def __init__(self):
        self.menu_urls = []

    async def set_chat_menu_button(self, menu_button):
        self.menu_urls.append(menu_button.web_app.url)


@pytest.fixture
def webapp(monkeypatch, tmp_path):
    """`start_webapp` без сети: сервер, туннель и перевешивание кнопок подменены."""
    from src.bot import main
    from src.bot.handlers import common
    from src.webapp import server

    started = []

    class OwnTunnel:
        def __init__(self, binary, local_url):
            pass

        async def start(self):
            started.append(1)
            return OTHER

    async def start_server(bot, settings):
        return object()

    async def refresh_menu_buttons(bot):
        return None

    monkeypatch.setattr(server, "start_server", start_server)
    monkeypatch.setattr(mod, "Tunnel", OwnTunnel)
    monkeypatch.setattr(mod, "ensure_binary", lambda target: target)
    monkeypatch.setattr(mod, "restart_self", lambda: None)
    monkeypatch.setattr(common, "refresh_menu_buttons", refresh_menu_buttons)
    monkeypatch.setattr(main, "set_webapp_url", lambda url: None)
    monkeypatch.setattr(main.settings, "webapp_url", "")
    monkeypatch.setattr(main.settings, "webapp_tunnel", True)
    monkeypatch.setattr(main.settings, "webapp_tunnel_wait_s", 0.2)
    monkeypatch.setattr(main.settings, "webapp_tunnel_file", str(tmp_path / "tunnel.url"))
    yield main, started, tmp_path / "tunnel.url"
    for task in list(main._background):  # сторож адреса живёт, пока жив бот
        task.cancel()


async def test_the_bot_takes_the_address_from_the_service(webapp):
    main, started, url_file = webapp
    url_file.write_text(URL + "\n")
    bot = _Bot()

    runner, tunnel = await main.start_webapp(bot)

    assert bot.menu_urls == [URL]
    assert tunnel is None and started == []  # своего cloudflared бот не поднимал
    assert runner is not None


async def test_without_the_service_the_bot_still_gets_an_address(webapp):
    """Служба не установлена или не поднялась — приложение не должно пропасть."""
    main, started, url_file = webapp
    bot = _Bot()

    runner, tunnel = await main.start_webapp(bot)

    assert bot.menu_urls == [OTHER]
    assert tunnel is not None and started == [1]


async def test_an_own_domain_ignores_the_tunnel_file(webapp, monkeypatch):
    main, started, url_file = webapp
    url_file.write_text(URL + "\n")
    monkeypatch.setattr(main.settings, "webapp_url", "https://app.example.com")
    bot = _Bot()

    await main.start_webapp(bot)

    assert bot.menu_urls == ["https://app.example.com"]
    assert started == []


# ------------------------------------------------------------------ выкладка


def _unit(name: str) -> str:
    return (REPO / "deploy" / "systemd" / name).read_text()


def test_the_bot_and_the_service_agree_on_the_file():
    line = re.compile(r"^Environment=WEBAPP_TUNNEL_FILE=(\S+)$", re.M)
    bot, service = line.search(_unit("reelbot-bot.service")), line.search(_unit("reelbot-tunnel.service"))
    assert bot and service and bot.group(1) == service.group(1)
    assert "-m src.webapp.tunnel" in _unit("reelbot-tunnel.service")


def test_restarting_the_bot_does_not_restart_the_tunnel():
    """`Requires`/`PartOf`/`BindsTo` тянули бы перезапуск с бота на туннель и обратно."""
    bot, service = _unit("reelbot-bot.service"), _unit("reelbot-tunnel.service")
    assert re.search(r"^Wants=.*reelbot-tunnel\.service", bot, re.M)
    assert re.search(r"^After=.*reelbot-tunnel\.service", bot, re.M)
    for unit, other in ((bot, "reelbot-tunnel"), (service, "reelbot-bot")):
        for tie in ("Requires", "PartOf", "BindsTo"):
            assert not re.search(rf"^{tie}=.*{other}", unit, re.M), f"{tie} на {other}"


def test_a_deploy_keeps_the_running_tunnel():
    """Выкладка перезапускает туннель, только если поменялась сама его служба."""
    script = (REPO / "deploy" / "update.sh").read_text()
    restarts = [
        ln
        for ln in script.splitlines()
        if "restart" in ln and "reelbot-tunnel" in ln and not ln.lstrip().startswith("#")
    ]
    assert restarts, "туннель должен перезапускаться хотя бы при смене службы"
    assert all("TUNNEL_CHANGED" in ln for ln in restarts), restarts
    assert "enable --now reelbot-tunnel" in script
