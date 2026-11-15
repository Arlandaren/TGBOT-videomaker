"""Звук рилса Instagram по ссылке — для подложки «Музыка из рилса».

Человек вставляет ссылку в Mini App, сервер сразу достаёт звук и кладёт его
файлом в папку приёма. Дальше трек живёт как файл задачи (`Job.music_key`), а
сводит его `audio_post.build_audio`.

Проверено на боевом сервере 9 ноября 2026: Instagram отдаёт публичные рилсы без
входа, звук лежит отдельной дорожкой m4a, на рилс уходит около трёх секунд.
Бывают рилсы вовсе без звука — это не поломка, а свой ответ человеку.

**Граница доверия.** yt-dlp умеет тысячу сайтов, `file://` и внутренние адреса.
Поэтому до него доходит только ссылка, СОБРАННАЯ из разобранного кода рилса на
`https://www.instagram.com`, а не строка пользователя. Короткую ссылку
«Поделиться» раскрываем сами одним запросом без автопереходов, и её `Location`
проходит те же правила.

yt-dlp запускается отдельным процессом: таймаут убивает его целиком, а цикл
событий веб-приложения не ждёт сети Instagram.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# Чем запускать yt-dlp. Списком, чтобы тесты подменяли его двойником.
YT_DLP: list[str] = [sys.executable, "-m", "yt_dlp"]
# Рилс на сервере качается за три секунды; 45 — запас на медленный Instagram,
# но не столько, чтобы человек ждал ответа поля минуту.
REEL_FETCH_TIMEOUT_S: float = 45
# Звук рилса — десятки секунд AAC на 60–130 кбит/с, то есть сотни килобайт.
# 25 МБ хватает на полчаса звука и не дают раздуть диск чужим «рилсом».
REEL_MAX_BYTES = 25 * 1024 * 1024
SHARE_TIMEOUT_S = 10.0

Code = Literal["bad_link", "no_audio", "unavailable", "instagram_down", "too_big",
               "unsupported", "not_media"]

_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}
_CANDIDATE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_CODE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_KINDS = {"reel": "reel", "reels": "reel", "p": "p", "tv": "tv"}


class ReelAudioError(Exception):
    """Звук не достался. `code` — какой текст показать человеку."""

    def __init__(self, code: Code, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class ReelLink:
    kind: Literal["media", "share"]
    url: str


@dataclass(frozen=True)
class ReelTrack:
    path: Path
    duration: float


def _parse_one(candidate: str) -> ReelLink | None:
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if parts.username or parts.password or port is not None:
        return None
    if (parts.hostname or "").lower() not in _HOSTS:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if any(s in (".", "..") for s in segs):
        return None
    if segs and segs[0] == "share":
        rest = segs[1:]
        if len(rest) == 2 and rest[0] in ("reel", "p"):
            code_path = f"{rest[0]}/{rest[1]}"
            code = rest[1]
        elif len(rest) == 1:
            code_path = code = rest[0]
        else:
            return None
        if not _CODE.match(code):
            return None
        return ReelLink("share", f"https://www.instagram.com/share/{code_path}/")
    # `/<автор>/reel/<код>` — та же ссылка с именем автора впереди.
    if len(segs) == 3:
        segs = segs[1:]
    if len(segs) != 2 or segs[0] not in _KINDS or not _CODE.match(segs[1]):
        return None
    return ReelLink("media", f"https://www.instagram.com/{_KINDS[segs[0]]}/{segs[1]}/")


def parse_reel_link(raw: str) -> ReelLink | None:
    """Первая ссылка Instagram в строке → каноническая ссылка. None — не рилс.

    Ищем ссылку внутри текста: «Поделиться» в Telegram и заметках часто
    прикладывает к ней пару слов.
    """
    for candidate in _CANDIDATE.findall(raw or ""):
        link = _parse_one(candidate.strip(".,;!?)»\"'"))
        if link is not None:
            return link
    return None


async def resolve_share(link: ReelLink, *, client: httpx.AsyncClient | None = None) -> ReelLink:
    """Короткая ссылка «Поделиться» → ссылка на сам рилс.

    Один запрос без автопереходов: переход, ведущий за пределы Instagram, не
    выполняется вовсе, а просто не проходит разбор.
    """
    if link.kind == "media":
        return link
    own = client is None
    client = client or httpx.AsyncClient(timeout=SHARE_TIMEOUT_S)
    try:
        resp = await client.get(link.url, follow_redirects=False, timeout=SHARE_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise ReelAudioError("instagram_down", f"share: {e}") from e
    finally:
        if own:
            await client.aclose()
    target = parse_reel_link(resp.headers.get("location", ""))
    if target is None or target.kind != "media":
        raise ReelAudioError("bad_link", f"share → {resp.status_code}")
    return target


def _classify(stderr: str) -> Code | None:
    low = stderr.lower()
    # Лимит Instagram на анонимные запросы первым: в его тексте есть и «login»,
    # и «not available», и без этой строки человек слышал бы «рилс закрыт»,
    # а мы теряли бы ровно тот сигнал, ради которого держим лимит у себя.
    if "rate-limit" in low or "rate limit" in low:
        return "instagram_down"
    if "requested format is not available" in low or "no video in this post" in low:
        return "no_audio"
    if "larger than max-filesize" in low:
        return "too_big"
    if any(k in low for k in ("login", "private", "404", "not available", "does not exist",
                              "restricted")):
        return "unavailable"
    return None


async def fetch_reel_audio(url: str, dst_dir: Path, *, extractors: str = "Instagram") -> ReelTrack:
    """Скачивает звук рилса в `dst_dir/track.<ext>`. `url` — уже канонический.

    Пост с несколькими видео — плейлист: берём первый ролик, у которого есть
    звук (`--max-downloads 1` останавливает после первой удачи, yt-dlp тогда
    выходит с кодом 101).
    """
    from src.pipeline.audio_post import _audio_duration

    argv = [
        *YT_DLP, "--ignore-config", "--no-cache-dir", "--no-progress", "--no-warnings", "-q",
        "--socket-timeout", "15",
        "--max-filesize", str(REEL_MAX_BYTES),
        "--playlist-items", "1:10", "--max-downloads", "1",
        # Только модули своей площадки: универсальный режим yt-dlp сходил бы по
        # любому адресу, включая внутренние адреса сервера.
        "--use-extractors", extractors,
        # Последнее «/b» — файл, у которого звук заранее не виден (так отдаёт
        # TikTok): берём его и проверяем звук сами, ffprobe ниже.
        "-f", "ba/b[acodec!=none]/b",
        "--no-part",
        "-o", str(dst_dir / "track.%(ext)s"),
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=REEL_FETCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        for f in dst_dir.glob("track.*"):
            f.unlink(missing_ok=True)
        log.warning("reel_audio_broken: %s — yt-dlp не уложился в %s с", url, REEL_FETCH_TIMEOUT_S)
        raise ReelAudioError("instagram_down", "timeout") from None
    stderr = err.decode("utf-8", "replace")
    files = sorted(dst_dir.glob("track.*"))
    if not files or proc.returncode not in (0, 101):
        code = _classify(stderr)
        if code is None and proc.returncode in (0, 101):
            # Успешный выход без файла: с `-q` так молча пропускается файл
            # больше `--max-filesize` (проверено на 2026.10.26 — ни строки в
            # выводе, код 101).
            code = "too_big"
        if code is None:
            log.warning("reel_audio_broken: %s — код %s: %s", url, proc.returncode,
                        stderr.strip()[-500:])
            code = "instagram_down"
        else:
            log.warning("reel_audio %s: %s — %s", code, url, stderr.strip()[-300:])
        for f in files:
            f.unlink(missing_ok=True)
        raise ReelAudioError(code, stderr.strip()[-200:])
    path = files[0]
    fmt, _ = await asyncio.to_thread(_probe_media, path)
    if not _allowed_format(fmt):
        path.unlink(missing_ok=True)
        raise ReelAudioError("not_media", f"формат {fmt}")
    duration = await asyncio.to_thread(_audio_duration, path)
    if duration is None:
        path.unlink(missing_ok=True)
        raise ReelAudioError("no_audio", "в файле нет звука")
    return ReelTrack(path=path, duration=round(duration, 3))


# ------------------------------------------------------------------ любая ссылка
#
# 16 ноября 2026 владелец попросил принимать не только Instagram, а любую
# ссылку со звуком. Проверено с сервера: TikTok отдаёт звук (yt-dlp нужен
# `curl_cffi`, иначе TikTok отвечает «Unexpected response»), прямые ссылки на
# файлы работают, YouTube требует входа в Google-аккаунт («Sign in to confirm
# you're not a bot») — его честно не принимаем.

_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_TIKTOK_SHORT = {"vm.tiktok.com", "vt.tiktok.com"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
_TIKTOK_VIDEO = re.compile(r"^/@([A-Za-z0-9_.-]{1,64})/video/(\d{5,25})/?$")
_SHORT_CODE = re.compile(r"^/([A-Za-z0-9_-]{4,32})/?$")
# Какие модули yt-dlp запускать для площадки.
EXTRACTORS = {"instagram": "Instagram", "tiktok": "TikTok,vm.tiktok"}
FILE_HOPS = 4  # сколько переходов по редиректам допускаем для прямого файла
_MEDIA_TYPES = ("audio/", "video/", "application/octet-stream", "application/ogg", "application/mp4")
# Многие сайты отдают файл только «браузеру»: на `python-httpx` w3schools
# отвечал 403 (16.11.2026).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0 Safari/537.36")


@dataclass(frozen=True)
class AnyLink:
    """Разобранная ссылка. `kind`: instagram, instagram_share, tiktok, youtube, file."""

    kind: str
    url: str


def _parse_any(candidate: str) -> AnyLink | None:
    insta = _parse_one(candidate)
    if insta is not None:
        return AnyLink("instagram" if insta.kind == "media" else "instagram_share", insta.url)
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or port not in (None, 80, 443):
        return None
    host = parts.hostname.lower()
    if ".." in parts.path:
        return None
    if host in _TIKTOK_HOSTS:
        m = _TIKTOK_VIDEO.match(parts.path)
        return AnyLink("tiktok", f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}") if m else None
    if host in _TIKTOK_SHORT:
        m = _SHORT_CODE.match(parts.path)
        return AnyLink("tiktok", f"https://{host}/{m.group(1)}/") if m else None
    if host in _YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        return AnyLink("youtube", candidate)
    if host in _HOSTS:
        return None  # Instagram, но не рилс и не пост
    if parts.path in ("", "/") or "." not in host:
        # У файла всегда есть путь. Без этого условия недописанная ссылка
        # («https://www.insta», пока её набирают) шла бы в сеть и съедала лимит.
        return None
    return AnyLink("file", candidate)


def parse_link(raw: str) -> AnyLink | None:
    """Первая пригодная ссылка в строке. None — ссылки нет или она не годится."""
    for candidate in _CANDIDATE.findall(raw or ""):
        link = _parse_any(candidate.strip(".,;!?)»\"'"))
        if link is not None:
            return link
    return None


# Форматы, которые пускаем дальше ffprobe. Без списка ffprobe/ffmpeg угадывают
# формат сами, и HLS-плейлист под видом «видео» заставил бы их читать файлы
# сервера и ходить по ссылкам из плейлиста.
ALLOWED_FORMATS = {"mp3", "mov", "mp4", "m4a", "3gp", "ogg", "wav", "matroska", "webm", "aac", "flac"}


def _probe_media(path: Path) -> tuple[str | None, float | None]:
    """(формат, длительность) скачанного файла. ffprobe читает ТОЛЬКО сам файл
    (`-protocol_whitelist file`), никаких ссылок изнутри."""
    from src.core.proc import capture

    code, out, _ = capture(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file",
         "-show_entries", "format=format_name,duration", "-of", "default=nw=1", str(path)],
        timeout=30,
    )
    if code != 0:
        return None, None
    fmt = dur = None
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "format_name":
            fmt = value.strip()
        elif key == "duration":
            try:
                dur = float(value)
            except ValueError:
                pass
    return fmt, dur


def _allowed_format(fmt: str | None) -> bool:
    return bool(fmt) and bool(set(fmt.split(",")) & ALLOWED_FORMATS)


def _peer_ip(resp: httpx.Response) -> str | None:
    """Адрес, к которому соединение подключилось на самом деле."""
    try:
        stream = resp.extensions["network_stream"]
        addr = stream.get_extra_info("server_addr")
        return str(addr[0]) if addr else None
    except Exception:  # noqa: BLE001
        return None


def _check_peer(resp: httpx.Response) -> None:
    """Проверка по факту соединения. `_check_public` смотрит DNS до подключения,
    а подключение спрашивает DNS заново: домен с подменой (DNS rebinding) мог бы
    ответить публичным адресом на проверку и внутренним — на подключение."""
    import ipaddress

    ip = _peer_ip(resp)
    try:
        ok = ip is not None and ipaddress.ip_address(ip).is_global
    except ValueError:
        ok = False
    if not ok:
        log.warning("reel_audio: соединение ушло на непубличный адрес %s — отклонено", ip)
        raise ReelAudioError("bad_link", "непубличный адрес соединения")


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return sorted({info[4][0] for info in infos})


async def _check_public(url: str) -> None:
    """Только публичные адреса и стандартные порты — на КАЖДОМ переходе.

    Иначе ссылка «на файл» заставила бы сервер сходить к самому себе: к
    приложению, к хранилищу, к служебным адресам облака.
    """
    import ipaddress

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReelAudioError("bad_link", "не http(s)")
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ReelAudioError("bad_link", "порт или логин в ссылке")
    try:
        addrs = await _resolve(parts.hostname)
    except OSError as e:
        raise ReelAudioError("unavailable", f"адрес не найден: {e}") from e
    if not addrs or not all(ipaddress.ip_address(a).is_global for a in addrs):
        log.warning("reel_audio: ссылка на непубличный адрес отклонена: %s → %s", parts.hostname, addrs)
        raise ReelAudioError("bad_link", "непубличный адрес")


async def fetch_file_audio(url: str, dst_dir: Path, *, client: httpx.AsyncClient | None = None) -> ReelTrack:
    """Прямая ссылка на аудио или видео → `dst_dir/track.<ext>`, со своими проверками.

    Переходы по редиректам — вручную, каждый адрес проходит `_check_public`.
    Размер ограничен `REEL_MAX_BYTES` и по заголовку, и по факту.
    """
    from src.pipeline.audio_post import _audio_duration

    own = client is None
    # trust_env=False: прокси из окружения сервера не должен обходить проверки адресов.
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(REEL_FETCH_TIMEOUT_S, connect=10),
                                         headers={"User-Agent": BROWSER_UA}, trust_env=False)
    dst = dst_dir / "track.bin"
    try:
        async with asyncio.timeout(REEL_FETCH_TIMEOUT_S):
            for _ in range(FILE_HOPS + 1):
                await _check_public(url)
                async with client.stream("GET", url, follow_redirects=False) as resp:
                    _check_peer(resp)
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        url = str(httpx.URL(url).join(resp.headers["location"]))
                        continue
                    if resp.status_code >= 400:
                        raise ReelAudioError("unavailable", f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not ctype.startswith(_MEDIA_TYPES):
                        raise ReelAudioError("not_media", ctype or "без типа")
                    if int(resp.headers.get("content-length") or 0) > REEL_MAX_BYTES:
                        raise ReelAudioError("too_big", "по заголовку")
                    got = 0
                    with dst.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            got += len(chunk)
                            if got > REEL_MAX_BYTES:
                                raise ReelAudioError("too_big", "по факту")
                            fh.write(chunk)
                    break
            else:
                raise ReelAudioError("unavailable", "слишком много переходов")
    except TimeoutError:
        dst.unlink(missing_ok=True)
        raise ReelAudioError("instagram_down", "таймаут скачивания") from None
    except httpx.HTTPError as e:
        dst.unlink(missing_ok=True)
        raise ReelAudioError("instagram_down", str(e)) from e
    except ReelAudioError:
        dst.unlink(missing_ok=True)
        raise
    finally:
        if own:
            await client.aclose()
    fmt, _ = await asyncio.to_thread(_probe_media, dst)
    if not _allowed_format(fmt):
        dst.unlink(missing_ok=True)
        log.warning("reel_audio: формат %r не из списка — отклонён", fmt)
        raise ReelAudioError("not_media", f"формат {fmt}")
    duration = await asyncio.to_thread(_audio_duration, dst)
    if duration is None:
        dst.unlink(missing_ok=True)
        raise ReelAudioError("no_audio", "в файле нет звука")
    return ReelTrack(path=dst, duration=round(duration, 3))
