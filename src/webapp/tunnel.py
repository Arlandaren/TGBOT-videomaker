"""Публичный HTTPS-адрес для локального сервера через Cloudflare quick tunnel.

Telegram открывает Mini App только по https. На машине без домена и сертификата
адрес даёт `cloudflared tunnel --url …`: без аккаунта, без настройки, но на
КАЖДОМ запуске новый (`*.trycloudflare.com`). Поэтому бот после старта туннеля
перевешивает кнопку меню на свежий адрес — см. `bot/main.py`.

Ограничения бесплатного туннеля, под которые подстроен сервер: тело запроса не
больше 100 МБ (загрузка идёт кусками) и никакой гарантии доступности. Для
постоянной работы задайте WEBAPP_URL своего домена — туннель тогда не нужен.

Два способа держать туннель:

* внутри бота (`Tunnel`, по умолчанию) — адрес меняется на каждом перезапуске
  бота, то есть на каждой выкладке;
* отдельной службой (`python -m src.webapp.tunnel`, `WEBAPP_TUNNEL_FILE`) —
  cloudflared переживает перезапуск бота, адрес тот же. Служба пишет адрес в
  файл, бот его читает (`wait_for_url`) и следит за сменой (`watch_url`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import stat
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelError(RuntimeError):
    pass


def ensure_binary(target: Path) -> Path:
    """cloudflared из PATH, а если его нет — скачанный в `target`."""
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("скачиваю cloudflared в %s", target)
    part = target.with_suffix(".part")
    with urllib.request.urlopen(DOWNLOAD_URL, timeout=120) as resp, part.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    part.chmod(part.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    part.replace(target)
    return target


def restart_self() -> None:
    """Штатно завершить бота: он закроет polling, а systemd поднимет его заново."""
    os.kill(os.getpid(), signal.SIGTERM)


class Tunnel:
    def __init__(
        self, binary: Path, local_url: str, on_death: Callable[[], None] | None = None
    ) -> None:
        self.binary = binary
        self.local_url = local_url
        # Что делать, когда cloudflared умер сам. По умолчанию — перезапустить
        # процесс (бота); служба туннеля подставляет своё.
        self.on_death = on_death
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self._pump: asyncio.Task | None = None
        self._stopping = False

    async def start(self, timeout_s: float = 60.0) -> str:
        self.proc = await asyncio.create_subprocess_exec(
            str(self.binary), "tunnel", "--no-autoupdate", "--url", self.local_url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        found: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pump = asyncio.create_task(self._read(found))
        try:
            self.url = await asyncio.wait_for(asyncio.shield(found), timeout_s)
        except asyncio.TimeoutError as e:
            await self.stop()
            raise TunnelError("cloudflared не выдал адрес за отведённое время") from e
        log.info("туннель поднят: %s → %s", self.url, self.local_url)
        return self.url

    async def _read(self, found: asyncio.Future[str]) -> None:
        # Поток читается до конца жизни процесса, а не до первого адреса: иначе
        # заполненный буфер stderr остановит cloudflared, и туннель встанет.
        assert self.proc is not None and self.proc.stderr is not None
        async for raw in self.proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if not found.done():
                m = _URL.search(line)
                if m:
                    found.set_result(m.group(0))
            if " ERR " in line:
                log.warning("cloudflared: %s", line)
        code = await self.proc.wait()
        if not found.done():
            found.set_exception(TunnelError(f"cloudflared завершился с кодом {code}"))
        elif not self._stopping:
            # Адрес туннеля живёт ровно столько, сколько процесс cloudflared, и
            # поднять новый внутри работающего бота мало: у всех кнопок останется
            # мёртвый адрес. Поэтому завершаемся целиком — systemd поднимет бота
            # заново, а старт сам возьмёт свежий адрес и перевесит кнопки меню.
            log.error("туннель упал (код %s) — нужен перезапуск ради нового адреса", code)
            (self.on_death or restart_self)()

    async def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._pump:
            self._pump.cancel()


# ------------------------------------------------------------------ служба


def read_url(path: Path) -> str:
    """Адрес из файла службы или пустая строка, если целого адреса там нет."""
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    return text if _URL.fullmatch(text) else ""


async def wait_for_url(path: Path, timeout_s: float = 60.0, poll_s: float = 0.5) -> str:
    """Дождаться адреса от службы туннеля: на старте машины бот может её опередить."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        url = read_url(path)
        if url:
            return url
        if loop.time() >= deadline:
            raise TunnelError(f"в {path} нет адреса туннеля")
        await asyncio.sleep(poll_s)


async def watch_url(path: Path, current: str, poll_s: float = 5.0) -> None:
    """Перезапустить бота, когда служба выдала другой адрес.

    Пустой файл или его отсутствие — служба перезапускается, ждём. Новый адрес
    значит, что у всех кнопок бота остался мёртвый: как и при своём туннеле,
    бот завершается, а старт перевешивает кнопки на свежий адрес.
    """
    while True:
        await asyncio.sleep(poll_s)
        url = read_url(path)
        if url and url != current:
            log.error("адрес туннеля сменился (%s → %s) — перезапускаю бота", current, url)
            restart_self()
            return


async def serve(binary: Path, local_url: str, url_file: Path) -> int:
    """Держать туннель и его адрес в `url_file`, пока жив cloudflared.

    Возвращает 1, когда cloudflared умер сам: systemd поднимет службу заново, и
    она выдаст новый адрес. Файл существует только при живом туннеле — бот не
    должен прочитать адрес прошлого запуска.
    """
    url_file.unlink(missing_ok=True)
    died = asyncio.Event()
    tunnel = Tunnel(binary, local_url, on_death=died.set)
    try:
        url = await tunnel.start()
        url_file.parent.mkdir(parents=True, exist_ok=True)
        part = url_file.with_name(url_file.name + ".part")
        part.write_text(url + "\n")
        part.replace(url_file)
        await died.wait()
        return 1
    finally:
        url_file.unlink(missing_ok=True)
        await tunnel.stop()


def main() -> None:
    from src.core.config import settings
    from src.core.logging import setup_logging

    setup_logging(settings.log_level)
    url_file = settings.webapp_tunnel_file.strip()
    if not url_file:
        sys.exit("WEBAPP_TUNNEL_FILE не задан — службе некуда писать адрес")
    binary = ensure_binary(settings.cloudflared_bin)
    local_url = f"http://{settings.webapp_host}:{settings.webapp_port}"

    async def run() -> int:
        task = asyncio.ensure_future(serve(binary, local_url, Path(url_file)))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await task
        except asyncio.CancelledError:
            return 0  # остановили нас: файл уже убран, перезапуск не нужен

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
