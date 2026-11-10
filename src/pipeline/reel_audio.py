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

Code = Literal["bad_link", "no_audio", "unavailable", "instagram_down", "too_big"]

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
    if "requested format is not available" in low:
        return "no_audio"
    if "larger than max-filesize" in low:
        return "too_big"
    if any(k in low for k in ("login", "private", "404", "not available", "does not exist",
                              "restricted")):
        return "unavailable"
    return None


async def fetch_reel_audio(url: str, dst_dir: Path) -> ReelTrack:
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
        "-f", "ba/b[acodec!=none]",
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
        for f in files:
            f.unlink(missing_ok=True)
        raise ReelAudioError(code, stderr.strip()[-200:])
    path = files[0]
    duration = await asyncio.to_thread(_audio_duration, path)
    if duration is None:
        path.unlink(missing_ok=True)
        raise ReelAudioError("no_audio", "в файле нет звука")
    return ReelTrack(path=path, duration=round(duration, 3))
