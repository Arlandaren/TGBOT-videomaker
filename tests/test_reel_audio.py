"""Звук рилса по ссылке: разбор ссылки, раскрытие «Поделиться», скачивание.

Сеть в тестах не трогается. yt-dlp подменяется скриптом-двойником, который
ведёт себя как настоящий в нужном случае: кладёт файл, ругается в stderr,
зависает. Instagram — `httpx.MockTransport`.

Главное, что здесь держится, — что до yt-dlp доходит только ссылка, собранная
из разобранного кода. Строка пользователя туда не попадает никогда: иначе
сервер ходил бы по чужому адресу (`file://`, внутренняя сеть) от своего имени.
"""

from __future__ import annotations

import json
import logging
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from src.core.proc import ffmpeg
from src.pipeline import reel_audio
from src.pipeline.reel_audio import (
    ReelAudioError,
    ReelLink,
    ReelTrack,
    fetch_reel_audio,
    parse_reel_link,
    resolve_share,
)

# ------------------------------------------------------------------ разбор ссылки


@pytest.mark.parametrize("raw, want", [
    ("https://www.instagram.com/reel/DfXkZV0Sbuu/?utm_source=ig_web_copy_link&mdxt=NTc4",
     "https://www.instagram.com/reel/DfXkZV0Sbuu/"),
    ("https://instagram.com/reels/Cop84x6u7CP", "https://www.instagram.com/reel/Cop84x6u7CP/"),
    ("http://m.instagram.com/p/BQ0eAlwhDrw/?igsh=abc", "https://www.instagram.com/p/BQ0eAlwhDrw/"),
    ("https://www.instagram.com/marvelskies.fc/reel/CWqAgUZgCku/",
     "https://www.instagram.com/reel/CWqAgUZgCku/"),
    ("https://instagr.am/tv/BkfuX9UB-eK", "https://www.instagram.com/tv/BkfuX9UB-eK/"),
    ("Смотри какой звук https://www.instagram.com/reel/DfXkZV0Sbuu/ огонь",
     "https://www.instagram.com/reel/DfXkZV0Sbuu/"),
    ("  https://WWW.Instagram.com/reel/DfXkZV0Sbuu/#x  ", "https://www.instagram.com/reel/DfXkZV0Sbuu/"),
])
def test_a_reel_link_becomes_canonical(raw, want):
    assert parse_reel_link(raw) == ReelLink("media", want)


@pytest.mark.parametrize("raw, want", [
    ("https://www.instagram.com/share/reel/BAabc123/", "https://www.instagram.com/share/reel/BAabc123/"),
    ("https://instagram.com/share/BAabc123?igsh=x", "https://www.instagram.com/share/BAabc123/"),
    ("https://www.instagram.com/share/p/BAabc123/", "https://www.instagram.com/share/p/BAabc123/"),
])
def test_a_share_link_is_kept_for_resolving(raw, want):
    assert parse_reel_link(raw) == ReelLink("share", want)


@pytest.mark.parametrize("raw", [
    "",
    "просто текст",
    "https://www.tiktok.com/@a/video/1",
    "https://evil.com/reel/DfXkZV0Sbuu/",
    "https://instagram.com.evil.com/reel/DfXkZV0Sbuu/",
    "https://evil.com/?u=https://www.instagram.com/reel/DfXkZV0Sbuu/",
    "https://www.instagram.com/someuser/",
    "file:///etc/passwd",
    "ftp://www.instagram.com/reel/DfXkZV0Sbuu/",
    "https://www.instagram.com/reel/../../x/",
    "https://www.instagram.com/reel/a/",
    "https://user:pw@www.instagram.com/reel/DfXkZV0Sbuu/",
    "https://www.instagram.com:8443/reel/DfXkZV0Sbuu/",
])
def test_anything_else_is_not_a_reel(raw):
    assert parse_reel_link(raw) is None


# ---------------------------------------------------------- «Поделиться»


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_share_link_resolves_to_the_reel_it_points_at():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": "https://www.instagram.com/reel/DfXkZV0Sbuu/?igsh=x"}
        )

    async with _client(handler) as client:
        got = await resolve_share(
            ReelLink("share", "https://www.instagram.com/share/reel/BAabc123/"), client=client
        )
    assert got == ReelLink("media", "https://www.instagram.com/reel/DfXkZV0Sbuu/")
    assert seen == ["https://www.instagram.com/share/reel/BAabc123/"]


async def test_a_share_link_leading_outside_instagram_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.com/reel/DfXkZV0Sbuu/"})

    async with _client(handler) as client:
        with pytest.raises(ReelAudioError) as e:
            await resolve_share(ReelLink("share", "https://www.instagram.com/share/BAabc123/"),
                                client=client)
    assert e.value.code == "bad_link"


async def test_a_share_link_without_a_redirect_is_refused():
    async with _client(lambda r: httpx.Response(200, text="<html>")) as client:
        with pytest.raises(ReelAudioError) as e:
            await resolve_share(ReelLink("share", "https://www.instagram.com/share/BAabc123/"),
                                client=client)
    assert e.value.code == "bad_link"


async def test_instagram_not_answering_the_share_link_is_its_own_failure():
    def handler(request):
        raise httpx.ConnectError("нет сети", request=request)

    async with _client(handler) as client:
        with pytest.raises(ReelAudioError) as e:
            await resolve_share(ReelLink("share", "https://www.instagram.com/share/BAabc123/"),
                                client=client)
    assert e.value.code == "instagram_down"


# ------------------------------------------------------------------ скачивание

REEL = "https://www.instagram.com/reel/DfXkZV0Sbuu/"


@pytest.fixture(scope="module")
def audio_m4a(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("reel") / "sound.m4a"
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", str(path)],
           timeout=60)
    return path


@pytest.fixture(scope="module")
def video_only_mp4(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("reel") / "mute.mp4"
    ffmpeg(["-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1", "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], timeout=60)
    return path


FAKE = textwrap.dedent('''
    import json, os, shutil, sys, time
    argv = sys.argv[1:]
    with open(os.environ["FAKE_ARGV"], "w") as fh:
        json.dump(argv, fh)
    mode = os.environ["FAKE_MODE"]
    out = argv[argv.index("-o") + 1]
    def put(src, ext):
        shutil.copy(src, out.replace("%(ext)s", ext))
    if mode in ("ok", "ok101"):
        put(os.environ["FAKE_AUDIO"], "m4a")
        sys.exit(101 if mode == "ok101" else 0)
    if mode == "video_only":
        put(os.environ["FAKE_VIDEO"], "mp4")
        sys.exit(0)
    if mode == "sleep":
        time.sleep(5)
        sys.exit(0)
    if mode == "toobig":
        # Так ведёт себя настоящий 2026.10.26 с `-q`: файл пропущен молча,
        # `--max-downloads` засчитал попытку, код 101, файла нет.
        sys.exit(101)
    msgs = {
        "noformat": "ERROR: [Instagram] X: Requested format is not available. Use --list-formats",
        "private": "ERROR: [Instagram] X: This content may be inappropriate. Login required",
        "gone": "ERROR: [Instagram] X: Unable to download webpage: HTTP Error 404: Not Found",
        "garbage": "ERROR: [Instagram] X: Unable to extract data; please report this issue",
    }
    sys.stderr.write(msgs[mode] + "\\n")
    sys.exit(1)
''')


@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch, audio_m4a, video_only_mp4):
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(FAKE, encoding="utf-8")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setattr(reel_audio, "YT_DLP", [sys.executable, str(script)])
    monkeypatch.setenv("FAKE_ARGV", str(argv_file))
    monkeypatch.setenv("FAKE_AUDIO", str(audio_m4a))
    monkeypatch.setenv("FAKE_VIDEO", str(video_only_mp4))

    def use(mode: str) -> Path:
        monkeypatch.setenv("FAKE_MODE", mode)
        return argv_file

    return use


@pytest.mark.parametrize("mode", ["ok", "ok101"])
async def test_the_reel_sound_lands_in_the_folder(tmp_path, fake_ytdlp, mode):
    fake_ytdlp(mode)
    dst = tmp_path / "music"
    dst.mkdir()
    got = await fetch_reel_audio(REEL, dst)
    assert isinstance(got, ReelTrack)
    assert got.path.parent == dst
    assert got.path.exists()
    assert got.duration == pytest.approx(3.0, abs=0.1)


async def test_ytdlp_gets_only_the_canonical_link_and_hard_limits(tmp_path, fake_ytdlp):
    argv_file = fake_ytdlp("ok")
    await fetch_reel_audio(REEL, tmp_path)
    argv = json.loads(argv_file.read_text())
    for flag in ("--ignore-config", "--no-cache-dir", "--max-filesize", "--max-downloads", "-f"):
        assert flag in argv, flag
    assert argv[argv.index("--max-downloads") + 1] == "1"
    assert argv[argv.index("--max-filesize") + 1] == str(reel_audio.REEL_MAX_BYTES)
    assert argv[-1] == REEL


@pytest.mark.parametrize("mode, code", [
    ("noformat", "no_audio"),
    ("video_only", "no_audio"),
    ("private", "unavailable"),
    ("gone", "unavailable"),
    ("toobig", "too_big"),
])
async def test_each_failure_has_its_own_code(tmp_path, fake_ytdlp, mode, code):
    fake_ytdlp(mode)
    with pytest.raises(ReelAudioError) as e:
        await fetch_reel_audio(REEL, tmp_path)
    assert e.value.code == code


async def test_an_unknown_ytdlp_failure_is_logged_as_broken(tmp_path, fake_ytdlp, caplog):
    """Так выглядит поломка yt-dlp на новой вёрстке Instagram: её видно в логе
    отдельной меткой, а не теряется среди неверных ссылок."""
    fake_ytdlp("garbage")
    with caplog.at_level(logging.WARNING, logger=reel_audio.__name__):
        with pytest.raises(ReelAudioError) as e:
            await fetch_reel_audio(REEL, tmp_path)
    assert e.value.code == "instagram_down"
    assert "reel_audio_broken" in caplog.text


async def test_a_hung_download_is_killed(tmp_path, fake_ytdlp, monkeypatch):
    fake_ytdlp("sleep")
    monkeypatch.setattr(reel_audio, "REEL_FETCH_TIMEOUT_S", 0.5)
    with pytest.raises(ReelAudioError) as e:
        await fetch_reel_audio(REEL, tmp_path)
    assert e.value.code == "instagram_down"
    assert not list(tmp_path.glob("track.*"))


@pytest.mark.parametrize("stderr, code", [
    ("ERROR: [Instagram] X: Requested content is not available, rate-limit reached or login required. "
     "You have exceeded the rate-limit for accessing posts anonymously", "instagram_down"),
    ("ERROR: [Instagram] X: There is no video in this post", "no_audio"),
])
def test_the_rate_limit_and_photo_posts_are_told_apart(stderr, code):
    """Лимит Instagram — это «не ответил, попробуйте позже», а не «рилс закрыт»;
    пост с фотографиями — «нет звука», а не поломка yt-dlp."""
    assert reel_audio._classify(stderr) == code


async def test_every_failure_is_logged_with_its_code(tmp_path, fake_ytdlp, caplog):
    """Сигнал здоровья yt-dlp — все отказы с кодом, а не только неузнанные."""
    fake_ytdlp("private")
    with caplog.at_level(logging.WARNING, logger=reel_audio.__name__):
        with pytest.raises(ReelAudioError):
            await fetch_reel_audio(REEL, tmp_path)
    assert "unavailable" in caplog.text


# ------------------------------------------------ любая ссылка со звуком

from src.pipeline.reel_audio import AnyLink, fetch_file_audio, parse_link  # noqa: E402


@pytest.mark.parametrize("raw, kind, url", [
    ("https://www.instagram.com/reel/DfXkZV0Sbuu/?igsh=x", "instagram", "https://www.instagram.com/reel/DfXkZV0Sbuu/"),
    ("https://www.instagram.com/share/reel/BAabc123/", "instagram_share", "https://www.instagram.com/share/reel/BAabc123/"),
    ("https://www.tiktok.com/@audible/video/7537416115673173278?is_from_webapp=1",
     "tiktok", "https://www.tiktok.com/@audible/video/7537416115673173278"),
    ("смотри https://vm.tiktok.com/ZMabc123/ огонь", "tiktok", "https://vm.tiktok.com/ZMabc123/"),
    ("https://vt.tiktok.com/ZSxyz9/", "tiktok", "https://vt.tiktok.com/ZSxyz9/"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "youtube", ""),
    ("https://youtu.be/jNQXAC9IVRw", "youtube", ""),
    ("https://www.youtube.com/shorts/BGQWPY4IigY", "youtube", ""),
    ("https://cdn.example.com/music/track.mp3?sig=1", "file", "https://cdn.example.com/music/track.mp3?sig=1"),
    ("http://example.com/clip.mp4", "file", "http://example.com/clip.mp4"),
])
def test_any_link_is_recognised(raw, kind, url):
    got = parse_link(raw)
    assert got is not None and got.kind == kind
    if url:
        assert got.url == url


@pytest.mark.parametrize("raw", [
    "", "просто текст", "file:///etc/passwd", "ftp://example.com/a.mp3",
    "https://user:pw@example.com/a.mp3", "https://example.com:8443/a.mp3",
    "https://www.tiktok.com/@audible", "https://www.tiktok.com/../../x",
    "https://www.insta", "https://example.com/",
])
def test_no_usable_link(raw):
    assert parse_link(raw) is None


async def test_platform_links_run_only_their_own_extractors(tmp_path, fake_ytdlp):
    """Без универсального режима yt-dlp: он сходил бы по любому адресу."""
    argv_file = fake_ytdlp("ok")
    await fetch_reel_audio(REEL, tmp_path, extractors="TikTok,vm.tiktok")
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--use-extractors") + 1] == "TikTok,vm.tiktok"
    assert argv[argv.index("-f") + 1].endswith("/b"), "файл без явного звука тоже берём и проверяем сами"


# --- прямой файл: качаем сами, только с публичных адресов


@pytest.fixture
def public_dns(monkeypatch):
    table = {"cdn.example.com": ["93.184.216.34"], "evil.example.com": ["127.0.0.1"],
             "lan.example.com": ["10.0.0.5"], "meta.example.com": ["169.254.169.254"]}

    async def resolve(host):
        if host not in table:
            raise OSError("no such host")
        return table[host]

    monkeypatch.setattr(reel_audio, "_resolve", resolve)
    return table


def _file_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_direct_audio_file_is_downloaded(tmp_path, public_dns, audio_m4a):
    body = audio_m4a.read_bytes()

    def handler(request):
        assert request.url.host == "cdn.example.com"
        return httpx.Response(200, headers={"content-type": "audio/mp4"}, content=body)

    async with _file_client(handler) as c:
        got = await fetch_file_audio("https://cdn.example.com/a.m4a", tmp_path, client=c)
    assert got.path.parent == tmp_path and got.duration == pytest.approx(3.0, abs=0.1)


@pytest.mark.parametrize("host", ["evil.example.com", "lan.example.com", "meta.example.com"])
async def test_internal_addresses_are_never_requested(tmp_path, public_dns, host):
    asked = []

    async with _file_client(lambda r: asked.append(r) or httpx.Response(200)) as c:
        with pytest.raises(ReelAudioError) as e:
            await fetch_file_audio(f"https://{host}/a.mp3", tmp_path, client=c)
    assert e.value.code == "bad_link" and asked == []


async def test_a_redirect_into_the_internal_network_is_refused(tmp_path, public_dns):
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://evil.example.com/admin"})

    async with _file_client(handler) as c:
        with pytest.raises(ReelAudioError) as e:
            await fetch_file_audio("https://cdn.example.com/a.mp3", tmp_path, client=c)
    assert e.value.code == "bad_link"
    assert asked == ["https://cdn.example.com/a.mp3"], "по внутреннему адресу не ходили"


async def test_our_own_client_introduces_itself_as_a_browser(tmp_path, public_dns, audio_m4a, monkeypatch):
    seen = {}
    body = audio_m4a.read_bytes()

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, headers={"content-type": "audio/mp4"}, content=body)

    real = httpx.AsyncClient

    def patched(**k):
        return real(transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr(reel_audio.httpx, "AsyncClient", patched)
    await fetch_file_audio("https://cdn.example.com/a.m4a", tmp_path)
    assert seen["ua"] == reel_audio.BROWSER_UA


async def test_a_web_page_is_not_a_media_file(tmp_path, public_dns):
    async with _file_client(lambda r: httpx.Response(200, headers={"content-type": "text/html"},
                                                     content=b"<html>")) as c:
        with pytest.raises(ReelAudioError) as e:
            await fetch_file_audio("https://cdn.example.com/page", tmp_path, client=c)
    assert e.value.code == "not_media"


@pytest.mark.parametrize("declared", [True, False])
async def test_a_huge_file_is_cut_off(tmp_path, public_dns, monkeypatch, declared):
    monkeypatch.setattr(reel_audio, "REEL_MAX_BYTES", 1000)
    headers = {"content-type": "video/mp4"}
    if declared:
        headers["content-length"] = "5000"

    async with _file_client(lambda r: httpx.Response(200, headers=headers, content=b"x" * 5000)) as c:
        with pytest.raises(ReelAudioError) as e:
            await fetch_file_audio("https://cdn.example.com/big.mp4", tmp_path, client=c)
    assert e.value.code == "too_big"
    assert not list(tmp_path.glob("track.*"))
