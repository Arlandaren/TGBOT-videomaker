"""HTTP-сторона Mini App: авторизация, протокол загрузки кусками, ссылки на ролик.

Сюда вынесено то, что проверяется без базы и хранилища. Постановка задачи в
очередь (списание, S3, Redis) проверена прогоном на живом стенде.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.core.config import Settings
from src.webapp.auth import sign_init_data
from src.webapp.server import check_media_token, create_app, media_token

TOKEN = "123456:TEST-token"


def init_data(user_id: int = 42) -> str:
    return sign_init_data(
        {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Т"})},
        TOKEN,
    )


def auth(user_id: int = 42) -> dict[str, str]:
    return {"Authorization": f"tma {init_data(user_id)}"}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from src.db import repo

    async def no_user(_user_id):  # язык ответа — по клиенту; база этим ручкам не нужна
        return None

    monkeypatch.setattr(repo, "get_user", no_user)
    cfg = Settings(_env_file=None, bot_token=TOKEN, work_dir=tmp_path, webapp_chunk_mb=1,
                   webapp_max_upload_mb=5)
    app = create_app(bot=None, cfg=cfg)  # type: ignore[arg-type] — бот этим ручкам не нужен
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_api_without_signature_is_refused(client):
    r = await client.post("/api/uploads", json={"size": 10})
    assert r.status == 401


async def test_api_with_a_foreign_bot_signature_is_refused(client):
    forged = sign_init_data({"auth_date": str(int(time.time())), "user": '{"id": 42}'}, "1:other")
    r = await client.post("/api/uploads", json={"size": 10}, headers={"Authorization": f"tma {forged}"})
    assert r.status == 401


async def test_the_index_page_is_public(client):
    """Страницу отдаём без подписи: подпись появляется только внутри Telegram."""
    r = await client.get("/")
    assert r.status == 200
    assert "telegram-web-app.js" in await r.text()


async def test_a_file_over_the_limit_is_refused_before_any_byte(client):
    r = await client.post("/api/uploads", json={"size": 6 * 1024 * 1024}, headers=auth())
    assert r.status == 413


async def test_chunks_resume_from_where_the_server_stopped(client):
    body = bytes(range(256)) * 12  # 3072 байта
    r = await client.post("/api/uploads", json={"size": len(body), "name": "a.mp4"}, headers=auth())
    upload_id = (await r.json())["upload_id"]
    url = f"/api/uploads/{upload_id}"

    r = await client.put(f"{url}?offset=0", data=body[:1000], headers=auth())
    assert (await r.json())["received"] == 1000

    # Ответ потерялся, клиент шлёт тот же кусок снова — второй раз не дописываем.
    r = await client.put(f"{url}?offset=0", data=body[:1000], headers=auth())
    assert r.status == 200 and (await r.json())["received"] == 1000

    # Клиент «забежал вперёд» — сервер говорит, откуда продолжать, и не портит файл.
    r = await client.put(f"{url}?offset=2000", data=body[2000:], headers=auth())
    assert r.status == 409 and (await r.json())["received"] == 1000

    r = await client.put(f"{url}?offset=1000", data=body[1000:], headers=auth())
    assert (await r.json())["received"] == len(body)

    from src.webapp.server import CFG_KEY, _upload_dir

    data = (_upload_dir(client.server.app[CFG_KEY], upload_id) / "data").read_bytes()
    assert data == body


async def test_a_chunk_past_the_declared_size_is_refused(client):
    r = await client.post("/api/uploads", json={"size": 100}, headers=auth())
    upload_id = (await r.json())["upload_id"]
    r = await client.put(f"/api/uploads/{upload_id}?offset=0", data=b"x" * 101, headers=auth())
    assert r.status == 400


async def test_someone_elses_upload_is_invisible(client):
    r = await client.post("/api/uploads", json={"size": 100}, headers=auth(42))
    upload_id = (await r.json())["upload_id"]
    r = await client.put(f"/api/uploads/{upload_id}?offset=0", data=b"x" * 10, headers=auth(43))
    assert r.status == 404


async def test_a_user_cannot_keep_opening_uploads(client):
    """Приём файла занимает диск до самого конца загрузки, а брошенную папку
    убирает только почасовая уборка. При разрешённом гигабайте на файл десяток
    брошенных приёмов съедает весь свободный диск сервера — вместе с местом под
    Postgres, который на той же машине. Поэтому приёмов на пользователя
    считанное число."""
    from src.webapp.server import MAX_OPEN_UPLOADS

    ids = []
    for _ in range(MAX_OPEN_UPLOADS):
        r = await client.post("/api/uploads", json={"size": 1024}, headers=auth())
        assert r.status == 200
        ids.append((await r.json())["upload_id"])

    r = await client.post("/api/uploads", json={"size": 1024}, headers=auth())
    assert r.status == 429
    # Чужие приёмы в счёт не идут: лимит на пользователя, а не на сервер.
    assert (await client.post("/api/uploads", json={"size": 1024}, headers=auth(43))).status == 200


async def test_an_upload_that_does_not_fit_the_disk_is_refused_up_front(client, monkeypatch):
    """Отказ до первого байта, а не на середине загрузки: пользователь с
    гигабайтным файлом иначе узнаёт о нехватке места через десять минут."""
    import shutil

    from src.webapp import server as mod

    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(0, 0, 3 * 1024 * 1024)
    )
    r = await client.post("/api/uploads", json={"size": 2 * 1024 * 1024}, headers=auth())
    assert r.status == 507, "приём на диск, где места нет, разрешили"

    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(0, 0, 50 * 1024 * 1024 * 1024)
    )
    assert (await client.post("/api/uploads", json={"size": 2 * 1024 * 1024}, headers=auth())).status == 200


async def test_space_already_promised_to_other_uploads_is_not_promised_twice(client, monkeypatch):
    """Проверка «влезет ли файл» обязана считать и уже обещанное.

    Байты незавершённого приёма на диске ещё не лежат, поэтому свободного места
    по `disk_usage` сколько было, столько и есть. Лимит на пользователя тут не
    спасает: десять человек по два приёма — это двадцать обещанных гигабайт на
    диске, где их нет.
    """
    import shutil

    from src.webapp import server as mod

    free = 12 * 1024 * 1024
    monkeypatch.setattr(mod, "DISK_RESERVE_MB", 0)
    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(0, 0, free)
    )
    # Два приёма по 5 МБ от разных людей — обещано 10 из 12 МБ.
    for user in (42, 43):
        r = await client.post("/api/uploads", json={"size": 5 * 1024 * 1024}, headers=auth(user))
        assert r.status == 200

    r = await client.post("/api/uploads", json={"size": 5 * 1024 * 1024}, headers=auth(44))
    assert r.status == 507, "пообещали место, которое уже обещано другим приёмам"
    # А то, что в остаток влезает, принимается.
    assert (await client.post("/api/uploads", json={"size": 1024}, headers=auth(44))).status == 200


async def test_a_reservation_nobody_feeds_stops_holding_space(client, monkeypatch):
    """Заявка на место — не бронь навечно.

    Объявить гигабайт и не прислать ни байта стоит ничего, а место держит до
    уборщика. Пяти аккаунтов хватило бы, чтобы на два часа закрыть загрузку
    всем. Поэтому заявка живёт коротким сроком, который продлевает каждый
    пришедший кусок: кто молчит — место отпускает, и своей же квоты не
    занимает.
    """
    import os
    import shutil
    import time as _time

    from src.webapp import server as mod

    free = 5 * 1024 * 1024
    monkeypatch.setattr(mod, "DISK_RESERVE_MB", 0)
    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(0, 0, free)
    )
    cfg = client.server.app[mod.CFG_KEY]

    # Два приёма по 2 МБ — обещано 4 из 5, третий такой же не влезает.
    ids = []
    for user in (42, 43):
        r = await client.post("/api/uploads", json={"size": 2 * 1024 * 1024}, headers=auth(user))
        ids.append((await r.json())["upload_id"])
    assert (await client.post("/api/uploads", json={"size": 2 * 1024 * 1024}, headers=auth(44))).status == 507

    # Старим оба: кусков по ним не было дольше срока заявки.
    old = _time.time() - mod.UPLOAD_LEASE - 60
    for upload_id in ids:
        data = mod._upload_dir(cfg, upload_id) / "data"
        os.utime(data, (old, old))
        os.utime(data.parent, (old, old))

    r = await client.post("/api/uploads", json={"size": 2 * 1024 * 1024}, headers=auth(44))
    assert r.status == 200, "молчащая заявка продолжает держать место"
    # И своя же молчащая заявка не запирает квоту пользователя.
    assert (await client.post("/api/uploads", json={"size": 1024}, headers=auth(42))).status == 200


async def test_a_live_upload_keeps_its_reservation(client, monkeypatch):
    """Кусок пришёл — срок заявки пошёл заново, место остаётся за ней."""
    import shutil

    from src.webapp import server as mod

    monkeypatch.setattr(mod, "DISK_RESERVE_MB", 0)
    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(0, 0, 5 * 1024 * 1024)
    )
    r = await client.post("/api/uploads", json={"size": 4 * 1024 * 1024}, headers=auth(42))
    upload_id = (await r.json())["upload_id"]
    await client.put(f"/api/uploads/{upload_id}?offset=0", data=b"x" * 1024, headers=auth(42))

    r = await client.post("/api/uploads", json={"size": 2 * 1024 * 1024}, headers=auth(43))
    assert r.status == 507, "живой приём лишился своего места"


async def test_only_one_finisher_can_claim_an_upload(client):
    """Сборку приёма забирает ровно один — переименованием папки.

    Два «Готово» разом (двойной тап, повтор по тайм-ауту) иначе создали бы две
    задачи за один файл, то есть списали бы два ролика. И папка уходит из-под
    уборки на время сборки: дальше ffprobe и заливка, кусков больше не будет, а
    чужой `upload_start` имеет право снести просроченную заявку.
    """
    import asyncio as _asyncio

    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    r = await client.post("/api/uploads", json={"size": 4}, headers=auth())
    folder = mod._upload_dir(cfg, (await r.json())["upload_id"])

    claims = await _asyncio.gather(
        _asyncio.to_thread(mod._claim_upload, folder),
        _asyncio.to_thread(mod._claim_upload, folder),
    )
    won = [c for c in claims if c is not None]
    assert len(won) == 1, f"приём забрали дважды: {claims}"
    assert won[0].exists() and not folder.exists()


async def test_a_claimed_upload_is_out_of_the_sweepers_reach(client, monkeypatch):
    """Забранную папку уборка приёмов не видит, даже когда срок заявки истёк."""
    import os
    import time as _time

    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    r = await client.post("/api/uploads", json={"size": 4}, headers=auth())
    claimed = mod._claim_upload(mod._upload_dir(cfg, (await r.json())["upload_id"]))
    assert claimed is not None
    old = _time.time() - mod.UPLOAD_LEASE - 60
    os.utime(claimed / "data", (old, old))
    os.utime(claimed, (old, old))

    assert mod._sweep_stale_uploads(cfg) == []
    assert claimed.exists(), "уборка снесла приём, который прямо сейчас собирают"


async def test_a_refused_finish_leaves_no_folder_behind(client):
    """Отказ на сборке обязан убирать за собой файл.

    Забранная папка не попадает ни в квоту пользователя, ни под уборку заявок —
    в этом и смысл, пока сборка идёт. Но если отказ случился ПОСЛЕ того, как её
    забрали (не та тема, нет баланса, очередь полна), папка оставалась лежать до
    почасовой уборки и места не занимала ни в одном счёте. Цикл «залил —
    получил отказ» тогда кладёт на диск сколько угодно гигабайт.
    """
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    r = await client.post("/api/uploads", json={"size": 4}, headers=auth())
    upload_id = (await r.json())["upload_id"]
    await client.put(f"/api/uploads/{upload_id}?offset=0", data=b"abcd", headers=auth())

    r = await client.post(
        f"/api/uploads/{upload_id}/finish",
        json={"preset": "такой-темы-нет", "options": {}},
        headers=auth(),
    )
    assert r.status == 400

    left = list(Path(cfg.work_dir).glob("*"))
    assert left == [], f"после отказа на диске осталось: {[p.name for p in left]}"


async def test_an_abandoned_upload_is_swept_sooner_than_a_work_folder(tmp_path, monkeypatch):
    """Брошенная папка приёма — не папка задачи: за ней никто не придёт, а
    держит она до гигабайта. Сутки для неё слишком долго."""
    import os
    import time as _time

    from src.worker import tasks

    monkeypatch.setattr(tasks.settings, "work_dir", tmp_path)
    job_dir = tmp_path / "0000-job"
    upload_dir = tmp_path / "upload-deadbeef"
    for d in (job_dir, upload_dir):
        d.mkdir()
        (d / "data").write_bytes(b"x")
    # Папку, забранную под сборку, тоже подбираем по короткому сроку: сюда она
    # попадает только если процесс умер на середине, и ждать сутки нечего.
    claimed_dir = tmp_path / "taking-deadbeef"
    claimed_dir.mkdir()
    (claimed_dir / "data").write_bytes(b"x")
    # Всем по три часа: папка задачи обязана дожить до суток, приёмы — нет.
    old = _time.time() - 3 * 3600
    for d in (job_dir, upload_dir, claimed_dir):
        os.utime(d, (old, old))

    assert await tasks.cleanup_old_files({}) == 2
    assert job_dir.exists(), "уборка снесла папку живой задачи"
    assert not upload_dir.exists(), "брошенный приём остался занимать диск"
    assert not claimed_dir.exists(), "брошенная сборка осталась занимать диск"


@pytest.mark.parametrize("upload_id", ["../../etc", "zz", "0" * 31])
async def test_upload_ids_cannot_escape_the_work_dir(client, upload_id: str):
    r = await client.put(f"/api/uploads/{upload_id}?offset=0", data=b"x", headers=auth())
    assert r.status == 404


def test_media_links_expire_and_bind_to_the_job():
    now = 1_800_000_000
    t = media_token("job-1", TOKEN, ttl_s=60, now=now)
    assert check_media_token("job-1", t, TOKEN, now=now + 30)
    assert not check_media_token("job-1", t, TOKEN, now=now + 61), "ссылка обязана протухать"
    assert not check_media_token("job-2", t, TOKEN, now=now), "чужой ролик по той же ссылке"
    assert not check_media_token("job-1", t, "1:other", now=now)
    assert not check_media_token("job-1", "garbage", TOKEN, now=now)


async def test_errors_speak_the_users_language(client, monkeypatch):
    """Ответ API — на языке пользователя, а не русский всем подряд."""
    from src.db import repo

    class EnglishUser:
        lang = "en"
        language_code = "ru"

    async def english(_user_id):
        return EnglishUser()

    monkeypatch.setattr(repo, "get_user", english)
    r = await client.post("/api/uploads", json={"size": 0}, headers=auth())
    assert r.status == 400
    assert (await r.json())["error"] == "The file is empty."


async def test_a_dead_tunnel_restarts_the_bot_and_a_stopped_one_does_not(monkeypatch, tmp_path):
    """Туннель умер сам — процесс бота завершается, чтобы systemd поднял его с новым адресом.

    Остановленный нами туннель (штатное завершение бота) так делать не должен.
    """
    import os
    import signal

    from src.webapp import tunnel as mod

    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\necho 'INF https://abc-def.trycloudflare.com' >&2\nsleep \"$SLEEP\"\n")
    fake.chmod(0o755)
    kills = []
    # Подменяется только перезапуск бота: `os.kill` трогать нельзя — через него
    # asyncio останавливает сам дочерний процесс.
    monkeypatch.setattr(mod, "restart_self", lambda: kills.append((os.getpid(), signal.SIGTERM)))

    monkeypatch.setenv("SLEEP", "0.3")
    t = mod.Tunnel(fake, "http://127.0.0.1:1")
    assert await t.start(timeout_s=5) == "https://abc-def.trycloudflare.com"
    await t._pump
    assert kills == [(os.getpid(), signal.SIGTERM)]

    kills.clear()
    monkeypatch.setenv("SLEEP", "5")
    t = mod.Tunnel(fake, "http://127.0.0.1:1")
    await t.start(timeout_s=5)
    await t.stop()
    assert kills == []


# ------------------------------------------------------------------ шрифты


@pytest.fixture
async def booted(tmp_path, monkeypatch):
    """Клиент с ботом-двойником: `/api/bootstrap` спрашивает у бота его имя.

    База тоже двойник, и это не экономия: `upsert_user` держит движок
    SQLAlchemy глобально, привязанным к тому циклу событий, в котором его
    создали. Второй тест с этой фикстурой получал «Future attached to a
    different loop» и 500 — то есть проверки бутстрапа работали, пока он был
    ровно один. Постгрес нужен денежным тестам, а не этим.
    """
    from types import SimpleNamespace

    from src.db import repo
    from src.webapp.server import create_app

    async def no_user(_user_id):
        return None

    async def upsert(user_id, **fields):
        return SimpleNamespace(
            id=user_id, balance=3, videos_done=1, preset="pulse", settings={}, lang=None, ref_credit=0,
            username=fields.get("username"), first_name=fields.get("first_name"),
            language_code=fields.get("language_code"), is_admin=fields.get("is_admin", False),
        )

    async def me():
        return SimpleNamespace(username="bot", first_name="Бот")

    monkeypatch.setattr(repo, "get_user", no_user)
    monkeypatch.setattr(repo, "upsert_user", upsert)
    cfg = Settings(_env_file=None, bot_token=TOKEN, work_dir=tmp_path, webapp_chunk_mb=1,
                   webapp_max_upload_mb=5)
    app = create_app(bot=SimpleNamespace(me=me), cfg=cfg)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_bootstrap_offers_no_font_choice(booted):
    """Отдельный выбор шрифта убран: шрифт задаёт стиль.

    Пока массив шрифтов приходил, приложение рисовало по нему вкладку, а
    выбранный там вариант перекрывал шрифт у ВСЕХ стилей — они выглядели
    одинаково."""
    r = await booted.get("/api/bootstrap", headers=auth())
    assert r.status == 200
    assert "fonts" not in await r.json()


async def test_the_font_route_serves_the_whole_library(client):
    """Раньше список собирался только из пресетов. Шрифт, который не назвала ни
    одна тема, браузер бы не получил — и чип выбора показывал бы не тот шрифт."""
    from src.core.options import FONTS

    for key, (_, choice) in FONTS.items():
        if choice is None:
            continue
        r = await client.get(f"/fonts/{choice.file}")
        assert r.status == 200, f"{key}: шрифт не отдаётся приложению"
        assert (await r.read())[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO")


async def test_the_font_route_still_refuses_anything_else(client):
    for name in ("../../etc/passwd", "secret.env", "Unknown-Bold.ttf"):
        assert (await client.get(f"/fonts/{name}")).status == 404


async def test_fonts_travel_compressed(client):
    """Библиотека выбора — восемнадцать начертаний, и все рисуются на первом
    экране: плитка темы её шрифтом, чип образцом «Аа». В TTF это 3.1 МБ на
    открытие, в gzip — вдвое меньше. Распакованное обязано совпасть с файлом
    байт в байт: раскладку Python считает по ЭТОМУ файлу."""
    import gzip

    from src.core.config import REPO_ROOT

    name = "Forum-Regular.ttf"
    r = await client.get(f"/fonts/{name}", headers={"Accept-Encoding": "gzip"})
    assert r.status == 200
    # aiohttp распаковывает сам, поэтому сжатие видно по заголовку, а не по телу.
    assert r.headers.get("Content-Encoding") == "gzip"
    body = await r.read()
    raw = (REPO_ROOT / "assets" / "fonts" / name).read_bytes()
    assert body == raw
    assert len(gzip.compress(raw, 9)) < len(raw) * 0.75, "сжатие не окупается — проверить формат"

    plain = await client.get(f"/fonts/{name}", headers={"Accept-Encoding": "identity"})
    assert plain.status == 200 and await plain.read() == raw


# ------------------------------------------------------------------ темы


async def test_bootstrap_describes_every_theme_the_way_the_preview_draws_it(booted):
    """Превью рисует тему ЭТИМИ числами, а не своими.

    Пока их не было, картинка в приложении показывала одну композицию на все
    темы: один кегль, одно место в кадре, одно выделение. Тем стало
    семнадцать, и расхождение превью с роликом перестало быть мелочью —
    человек выбирает по картинке. Числа обязаны быть числами ТЕМЫ, иначе
    приложение обещает не то, что соберёт воркер.
    """
    from src.core.geometry import load_metrics
    from src.core.preset import available, load_preset

    data = await (await booted.get("/api/bootstrap", headers=auth())).json()
    assert {p["name"] for p in data["presets"]} == set(available())
    for item in data["presets"]:
        p = load_preset(item["name"])
        assert item["size"] == p.font.size, item["name"]
        assert item["uppercase"] == p.font.uppercase, item["name"]
        assert item["letter_spacing"] == p.font.letter_spacing, item["name"]
        # Зазор между словами превью берёт у темы, а не из своей константы:
        # иначе у темы с узким пробелом превью обещает широкий.
        space = load_metrics(p.font.file).advance(" ") / load_metrics(p.font.file).units_per_em
        assert item["word_gap"] == pytest.approx(
            space + p.font.letter_spacing + p.font.word_spacing, abs=1e-4
        ), item["name"]
        assert item["line_height"] == p.caption.line_height, item["name"]
        assert item["position"] == p.caption.position, item["name"]
        assert item["margin"] == p.caption.margin, item["name"]
        assert item["accent_scale"] == p.animation.accent_scale, item["name"]
        assert item["panel"]["enabled"] == p.panel.enabled, item["name"]
        assert item["panel"]["opacity"] == p.panel.opacity, item["name"]
        # Высота прописных — из выгруженных метрик, а не из числа, переписанного
        # руками: по ней приложение пересчитывает кегль при смене шрифта ровно
        # так же, как это делает `options.apply_font` в пайплайне.
        m = load_metrics(p.font.file)
        assert item["cap_em"] == pytest.approx(m.cap_height / m.units_per_em, abs=1e-4)
        # Второй шрифт темы: без него превью рисует пару одним шрифтом.
        if p.accent_font is None:
            assert item["accent_font"] is None, item["name"]
        else:
            assert item["accent_font"] == {
                "family": p.accent_font.family,
                "file": p.accent_font.file,
                "weight": p.accent_font.weight,
                "scale": p.accent_font.scale,
            }, item["name"]
    assert any(item["accent_font"] for item in data["presets"])


async def test_the_font_route_serves_the_second_font_of_a_theme(client):
    """Второго шрифта нет в каталоге выбора — его называет только тема."""
    from src.core.options import FONTS
    from src.core.preset import available, load_preset

    catalogued = {choice.file for _, choice in FONTS.values() if choice}
    files = {
        load_preset(n).accent_font.file for n in available() if load_preset(n).accent_font
    }
    assert files and not files <= catalogued
    for name in files:
        r = await client.get(f"/fonts/{name}")
        assert r.status == 200, f"{name}: второй шрифт темы не отдаётся приложению"


async def test_themes_come_in_one_order_for_the_app_and_the_chat(booted):
    """Порядок тем — свойство данных (`order`), и он один на оба входа.

    В чате и в приложении список один и тот же; разойдись они, «третья слева»
    значило бы разное. Тема по умолчанию при этом всегда первая — с неё
    начинает тот, кто ничего не выбирал.
    """
    from src.bot.keyboards import _titles
    from src.core.preset import load_preset

    data = await (await booted.get("/api/bootstrap", headers=auth())).json()
    names = [p["name"] for p in data["presets"]]
    assert names[0] == Settings(_env_file=None, bot_token=TOKEN).default_preset
    assert names[1:] == [n for n, _ in _titles() if n != names[0]]
    assert [load_preset(n).order for n in names[1:]] == sorted(
        load_preset(n).order for n in names[1:]
    )


def test_today_starts_at_the_users_midnight_not_the_servers():
    """«+N сегодня» в приглашениях считается от полуночи человека."""
    from datetime import datetime, timezone

    from aiohttp.test_utils import make_mocked_request

    from src.webapp.server import _day_start

    now = datetime(2026, 11, 3, 20, 30, tzinfo=timezone.utc)
    # Владивосток (UTC+10): там уже 4 ноября, 06:30 — сутки начались в 14:00 UTC.
    vlad = make_mocked_request("GET", "/api/referrals?tz=-600")
    assert _day_start(vlad, now) == datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)
    # Нью-Йорк (UTC−5): 15:30 3 ноября — сутки начались в 05:00 UTC.
    ny = make_mocked_request("GET", "/api/referrals?tz=300")
    assert _day_start(ny, now) == datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    # Мусор и смещения больше суток — как UTC.
    for junk in ("abc", "99999"):
        bad = make_mocked_request("GET", f"/api/referrals?tz={junk}")
        assert _day_start(bad, now) == datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ два видео


async def _uploaded(client, body: bytes, user_id: int = 42) -> str:
    r = await client.post("/api/uploads", json={"size": len(body)}, headers=auth(user_id))
    upload_id = (await r.json())["upload_id"]
    await client.put(f"/api/uploads/{upload_id}?offset=0", data=body, headers=auth(user_id))
    return upload_id


@pytest.fixture
def finish_env(client, monkeypatch):
    """Всё, что `finish` трогает до списания: пользователь, очередь, ffprobe.

    Нижний файл читается как видео со звуком, верхний (`TOP!`) — нет.
    """
    from types import SimpleNamespace

    from src.db import repo
    from src.pipeline import probe as probe_mod

    seen = {"created": 0}

    async def upsert_user(*a, **k):
        return SimpleNamespace(id=42, is_blocked=False, is_admin=False, balance=3, lang="ru",
                               language_code="ru", interface_lang=None)

    async def no_jobs(*a, **k):
        return 0

    async def create_job(*a, **k):
        seen["created"] += 1
        raise AssertionError("списали ролик за отказ")

    def fake_probe(path):
        if Path(path).read_bytes() == b"TOP!":
            raise RuntimeError("не видео")
        return SimpleNamespace(duration=20.0, has_audio=True)

    monkeypatch.setattr(repo, "upsert_user", upsert_user)
    monkeypatch.setattr(repo, "active_jobs_count", no_jobs)
    monkeypatch.setattr(repo, "create_job", create_job)
    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    return seen


async def test_a_foreign_top_upload_is_not_found(client, finish_env):
    """Чужой верхний файл — 404, а свой нижний не забран и цел: его можно
    отправить снова с правильным верхним."""
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    bottom = await _uploaded(client, b"abcd")
    foreign = await _uploaded(client, b"efgh", user_id=43)

    r = await client.post(
        f"/api/uploads/{bottom}/finish",
        json={"preset": "glass", "options": {}, "top_upload_id": foreign},
        headers=auth(),
    )
    assert r.status == 404
    assert (mod._upload_dir(cfg, bottom) / "data").read_bytes() == b"abcd"
    assert finish_env["created"] == 0


async def test_an_unreadable_top_is_refused_before_charging(client, finish_env):
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    bottom = await _uploaded(client, b"abcd")
    top = await _uploaded(client, b"TOP!")

    r = await client.post(
        f"/api/uploads/{bottom}/finish",
        json={"preset": "glass", "options": {}, "top_upload_id": top},
        headers=auth(),
    )
    assert r.status == 415
    assert "верхнее" in (await r.json())["error"], "отказ не про верхнее видео — нижнее не прочиталось?"
    assert finish_env["created"] == 0
    left = list(Path(cfg.work_dir).glob("*"))
    assert left == [], f"после отказа на диске осталось: {[p.name for p in left]}"


# ------------------------------------------------------------------ ролик из нескольких видео


async def test_too_many_clips_are_refused_and_nothing_is_taken(client, finish_env):
    """Шестое видео — отказ до того, как забрано первое: загрузки целы."""
    from src.pipeline.join import MAX_CLIPS
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    first = await _uploaded(client, b"abcd")
    r = await client.post(
        f"/api/uploads/{first}/finish",
        json={"preset": "glass", "options": {}, "extra_upload_ids": ["0" * 32] * MAX_CLIPS},
        headers=auth(),
    )
    assert r.status == 400
    assert (mod._upload_dir(cfg, first) / "data").read_bytes() == b"abcd"
    assert finish_env["created"] == 0


async def test_the_same_upload_twice_is_not_two_clips(client, finish_env):
    """Один и тот же приём дважды (или сам первый) — не два куска ролика."""
    first = await _uploaded(client, b"abcd")
    second = await _uploaded(client, b"efgh")
    for extra in ([first], [second, second]):
        r = await client.post(
            f"/api/uploads/{first}/finish",
            json={"preset": "glass", "options": {}, "extra_upload_ids": extra},
            headers=auth(),
        )
        assert r.status == 404, extra
    assert finish_env["created"] == 0


async def test_clips_do_not_mix_with_two_videos_mode(client, finish_env):
    first = await _uploaded(client, b"abcd")
    second = await _uploaded(client, b"efgh")
    top = await _uploaded(client, b"ijkl")
    r = await client.post(
        f"/api/uploads/{first}/finish",
        json={"preset": "glass", "options": {}, "top_upload_id": top, "extra_upload_ids": [second]},
        headers=auth(),
    )
    assert r.status == 400
    assert finish_env["created"] == 0


async def test_an_unreadable_clip_is_refused_before_charging(client, finish_env):
    """Второй файл — не видео: отказ называет его номер, баланс не тронут, на
    диске ничего не осталось."""
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    first = await _uploaded(client, b"abcd")
    second = await _uploaded(client, b"TOP!")
    r = await client.post(
        f"/api/uploads/{first}/finish",
        json={"preset": "glass", "options": {}, "extra_upload_ids": [second]},
        headers=auth(),
    )
    assert r.status == 415
    assert "№2" in (await r.json())["error"]
    assert finish_env["created"] == 0
    left = list(Path(cfg.work_dir).glob("*"))
    assert left == [], f"после отказа на диске осталось: {[p.name for p in left]}"


async def test_the_length_limit_is_for_all_clips_together(client, finish_env, monkeypatch):
    """Каждый кусок короче предела, вместе — длиннее: отказ, как у одного
    длинного видео. Иначе пределом длины был бы только размер кошелька."""
    from types import SimpleNamespace

    from src.pipeline import probe as probe_mod
    from src.pipeline import split
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    each = cfg.max_video_seconds * 0.6
    monkeypatch.setattr(probe_mod, "probe", lambda path: SimpleNamespace(duration=each, has_audio=True))
    monkeypatch.setattr(split, "top_video_seconds", lambda path: each)

    first = await _uploaded(client, b"abcd")
    second = await _uploaded(client, b"efgh")
    r = await client.post(
        f"/api/uploads/{first}/finish",
        json={"preset": "glass", "options": {}, "extra_upload_ids": [second]},
        headers=auth(),
    )
    assert r.status == 400
    assert "длиннее" in (await r.json())["error"]
    assert finish_env["created"] == 0


async def test_several_clips_become_one_job_with_parts(client, finish_env, monkeypatch):
    """Три видео — одна задача и одно списание: первое ложится исходником,
    остальные — кусками по порядку, а склейку делает воркер."""
    import uuid
    from types import SimpleNamespace

    from src.db import repo
    from src.pipeline import split
    from src.webapp import server as mod
    from src.worker import tasks

    job_id = uuid.uuid4()
    created: list[dict] = []
    updates: dict = {}
    uploaded: dict[str, bytes] = {}

    async def create_job(user_id, **k):
        created.append(k)
        return SimpleNamespace(id=job_id, **{"status": "queued"})

    async def update_job(_id, **values):
        updates.update(values)

    async def upload(path, key, **_):
        uploaded[key] = Path(path).read_bytes()

    async def boom(*a, **k):
        raise RuntimeError("дальше очереди тест не идёт")

    monkeypatch.setattr(repo, "create_job", create_job)
    monkeypatch.setattr(repo, "update_job", update_job)
    monkeypatch.setattr(repo, "mark_failed", lambda *a, **k: _none())
    monkeypatch.setattr(repo, "refund_job", lambda *a, **k: _none())
    monkeypatch.setattr(mod.storage, "upload", upload)
    monkeypatch.setattr(repo, "set_user_preset", boom)
    monkeypatch.setattr(split, "top_video_seconds", lambda path: 20.0)

    ids = [await _uploaded(client, body) for body in (b"one!", b"two!!", b"three!")]
    r = await client.post(
        f"/api/uploads/{ids[0]}/finish",
        json={"preset": "glass", "options": {}, "extra_upload_ids": ids[1:]},
        headers=auth(),
    )
    # Тест обрывает запрос сразу после заливки — нас интересует, что легло в хранилище.
    assert r.status == 500
    assert len(created) == 1, "несколько видео — одна задача и одно списание"
    assert created[0]["source_size"] == len(b"one!") + len(b"two!!") + len(b"three!")
    assert updates["source_parts"] == 2
    assert updates["source_duration"] == 60.0, "длина ролика — сумма кусков"
    assert uploaded[tasks.part_key(job_id, 1)] == b"two!!"
    assert uploaded[tasks.part_key(job_id, 2)] == b"three!"
    assert [v for k, v in uploaded.items() if k.endswith("source.mp4")] == [b"one!"]
    cfg = client.server.app[mod.CFG_KEY]
    assert list(Path(cfg.work_dir).glob("*")) == []


async def _none():
    return None


async def test_a_user_can_open_two_uploads_for_one_reel(client):
    """Два файла одного ролика и один брошенный приём не упираются в квоту."""
    for _ in range(3):
        r = await client.post("/api/uploads", json={"size": 1024}, headers=auth())
        assert r.status == 200


async def test_a_picture_as_the_top_is_refused(client, finish_env, tmp_path):
    """Картинка проходит ffprobe, но верх из неё — ролик без верха."""
    import subprocess

    img = tmp_path / "a.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                    "-frames:v", "1", str(img)], check=True)
    bottom = await _uploaded(client, b"abcd")
    top = await _uploaded(client, img.read_bytes())
    r = await client.post(
        f"/api/uploads/{bottom}/finish",
        json={"preset": "glass", "options": {}, "top_upload_id": top},
        headers=auth(),
    )
    assert r.status == 415
    assert finish_env["created"] == 0


async def test_a_finished_bottom_outlives_a_long_top_upload(client):
    """Нижнее догружено, верхнее идёт дольше срока заявки — нижнее обязано
    дожить до «Готово». Иначе пользователь залил два файла и получил 404."""
    import os
    import time as _time

    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    bottom = await _uploaded(client, b"abcd")
    r = await client.post("/api/uploads", json={"size": 8}, headers=auth())
    top = (await r.json())["upload_id"]

    # Нижний кусок пришёл давно, верхнее с тех пор всё грузится.
    old = _time.time() - mod.UPLOAD_LEASE - 300
    os.utime(mod._upload_dir(cfg, bottom) / "data", (old, old))
    await client.put(f"/api/uploads/{top}?offset=0", data=b"efgh", headers=auth())

    await client.post("/api/uploads", json={"size": 1}, headers=auth(43))  # чья-то загрузка запускает уборку
    assert (mod._upload_dir(cfg, bottom) / "data").exists(), "уборка снесла догруженное нижнее"


async def test_an_abandoned_finished_upload_still_expires(client):
    """Продление — только пока у хозяина идёт другая загрузка: брошенный
    догруженный файл по-прежнему уходит по сроку."""
    import os
    import time as _time

    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    bottom = await _uploaded(client, b"abcd")
    old = _time.time() - mod.UPLOAD_LEASE - 300
    os.utime(mod._upload_dir(cfg, bottom) / "data", (old, old))
    await client.post("/api/uploads", json={"size": 1}, headers=auth(43))
    assert not mod._upload_dir(cfg, bottom).exists()


# ------------------------------------------------------------------ токены


async def test_bootstrap_sells_token_packages(booted):
    """Тарифы как у Brandly: токены, «до N роликов», популярный, честная выгода."""
    body = await (await booted.get("/api/bootstrap", headers=auth())).json()
    assert body["video_cost"] == 23
    packs = body["packages"]
    assert [(p["tokens"], p["stars"], p["rub"], p["videos"]) for p in packs] == [
        (240, 749, 1099, 10), (590, 1899, 2799, 25), (1170, 3599, 5299, 50),
    ]
    assert [p["popular"] for p in packs] == [False, True, False]
    # Выгода — от цены токена в самом маленьком пакете и только если она есть:
    # 590 за 2799 ₽ токен ДОРОЖЕ, чем 240 за 1099 ₽, — «выгоды» там нет.
    assert [p["save"] for p in packs] == [0, 0, 1]


async def test_an_invoice_is_for_tokens_at_the_current_price(booted):
    from src.webapp import server as mod

    seen = {}

    async def create_invoice_link(**k):
        seen.update(k)
        return "https://t.me/$inv"

    booted.server.app[mod.BOT_KEY].create_invoice_link = create_invoice_link
    r = await booted.post("/api/invoice", json={"tokens": 590}, headers=auth())
    assert r.status == 200 and (await r.json())["link"] == "https://t.me/$inv"
    assert seen["payload"] == "tokens:590" and seen["currency"] == "XTR"
    assert seen["prices"][0].amount == 1899
    assert "590 токенов" in seen["title"]

    r = await booted.post("/api/invoice", json={"tokens": 300}, headers=auth())
    assert r.status == 400


# ------------------------------------------------------------------ PayHot


class FakePayHotClient:
    def __init__(self):
        self.created = []
        self.euro_asked = []

    async def create_payment(self, **k):
        from src.payments.payhot import Created

        self.created.append(k)
        return Created(payment_id="pay-9", checkout_url="https://go.pay.hot/pay/x#t=s")

    async def euro_for_rubles(self, rub_minor, method="card"):
        self.euro_asked.append((rub_minor, method))
        return 1309 if method == "card" else 1434

    async def get_payment(self, payment_id):
        from src.payments.payhot import Status

        return Status(status="succeeded", amount_minor=109900, currency="RUB", order_ref="")


@pytest.fixture
async def paying(tmp_path, monkeypatch):
    """Приложение с ключом PayHot, двойником его API и базы."""
    import uuid
    from types import SimpleNamespace

    from src.db import repo
    from src.webapp import server as mod
    from src.webapp.server import create_app

    store: dict = {"orders": {}, "settled": []}

    async def upsert(user_id, **f):
        return SimpleNamespace(id=user_id, balance=0, videos_done=0, preset="pulse", settings={},
                               lang="ru", ref_credit=0, is_blocked=False,
                               username=None, first_name="Т", language_code="ru",
                               is_admin=False)

    async def no_user(_):
        return None

    async def create_order(**k):
        order = SimpleNamespace(id=uuid.uuid4(), payment_id=None, status="created", credited=False, **k)
        store["orders"][order.id] = order
        return order

    async def set_payment(order_id, payment_id):
        store["orders"][uuid.UUID(str(order_id))].payment_id = payment_id

    async def get_order(order_id):
        return store["orders"].get(uuid.UUID(str(order_id)))

    async def me():
        return SimpleNamespace(username="ClipLy_app", first_name="ClipLy")

    monkeypatch.setattr(repo, "upsert_user", upsert)
    monkeypatch.setattr(repo, "get_user", no_user)
    monkeypatch.setattr(repo, "create_payhot_order", create_order)
    monkeypatch.setattr(repo, "set_payhot_payment", set_payment)
    monkeypatch.setattr(repo, "get_payhot_order", get_order)

    async def no_recent_orders(user_id, *, since):
        return 0

    monkeypatch.setattr(repo, "count_payhot_orders", no_recent_orders)
    fake = FakePayHotClient()
    cfg = Settings(_env_file=None, bot_token=TOKEN, work_dir=tmp_path, payhot_api_key="phk_test",
                   payhot_poll_s=0)
    app = create_app(bot=SimpleNamespace(me=me), cfg=cfg)  # type: ignore[arg-type]
    app[mod.PAYHOT_KEY] = fake
    async with TestClient(TestServer(app)) as c:
        yield c, fake, store


async def test_bootstrap_lists_the_payhot_methods_and_stars(paying):
    c, _, _ = paying
    body = await (await c.get("/api/bootstrap", headers=auth())).json()
    # Кошельки — в самом конце, после звёзд (просьба владельца 16.11.2026).
    assert [m["key"] for m in body["pay_methods"]] == [
        "sbp", "card", "crypto", "stars", "applepay", "googlepay"]


async def test_bootstrap_without_a_key_offers_only_stars(booted):
    body = await (await booted.get("/api/bootstrap", headers=auth())).json()
    assert [m["key"] for m in body["pay_methods"]] == ["stars"]


async def test_a_crypto_order_is_billed_in_the_package_rubles(paying):
    c, fake, store = paying
    r = await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"}, headers=auth())
    assert r.status == 200
    body = await r.json()
    assert body["url"] == "https://go.pay.hot/pay/x#t=s"
    [order] = store["orders"].values()
    assert str(order.id) == body["order_id"]
    assert (order.user_id, order.tokens, order.rub, order.method, order.currency, order.amount_minor) == (
        42, 240, 1099, "crypto", "RUB", 109900)
    assert order.payment_id == "pay-9"
    [k] = fake.created
    assert k["order_ref"] == str(order.id) and k["amount_minor"] == 109900 and k["currency"] == "RUB"
    assert k["method"] == "crypto" and k["customer_id"] == "tg-42"
    assert k["success_url"] == "https://t.me/ClipLy_app"


async def test_a_card_order_is_billed_in_euros_from_payhots_estimate(paying):
    c, fake, store = paying
    r = await c.post("/api/payhot/orders", json={"tokens": 590, "method": "card"}, headers=auth())
    assert r.status == 200
    [order] = store["orders"].values()
    assert fake.euro_asked == [(279900, "card")]
    assert (order.currency, order.amount_minor, order.rub) == ("EUR", 1309, 2799)


@pytest.mark.parametrize("method", ["applepay", "googlepay"])
async def test_a_wallet_order_is_its_own_method_in_euros(paying, method):
    c, fake, store = paying
    r = await c.post("/api/payhot/orders", json={"tokens": 240, "method": method}, headers=auth())
    assert r.status == 200
    [order] = store["orders"].values()
    assert fake.euro_asked == [(109900, method)]
    assert (order.method, order.currency, order.amount_minor) == (method, "EUR", 1434)
    assert fake.created[0]["method"] == method


@pytest.mark.parametrize("body", [
    {"tokens": 300, "method": "crypto"},     # нет такого пакета
    {"tokens": 240, "method": "bank"},       # такого способа нет
    {"tokens": 240, "method": "stars"},      # звёзды — другой путь
    {"tokens": "x", "method": "crypto"},
])
async def test_a_wrong_order_is_refused_before_payhot(paying, body):
    c, fake, store = paying
    r = await c.post("/api/payhot/orders", json=body, headers=auth())
    assert r.status == 400
    assert fake.created == [] and store["orders"] == {}


async def test_crypto_below_its_minimum_is_refused(paying, monkeypatch):
    from src.webapp import server as mod

    c, fake, store = paying
    cfg = c.server.app[mod.CFG_KEY]
    monkeypatch.setattr(cfg, "payhot_crypto_min_amount_minor", 200000)
    r = await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"}, headers=auth())
    assert r.status == 400 and fake.created == []


async def test_the_order_status_settles_the_payment(paying, monkeypatch):
    from src.payments import orders
    from src.payments.orders import Settled

    c, fake, store = paying
    order_id = (await (await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"},
                                    headers=auth())).json())["order_id"]
    seen = []

    async def settle(oid, client):
        seen.append((str(oid), client))
        return Settled("succeeded", True, True, 240, 42)

    monkeypatch.setattr(orders, "settle", settle)
    r = await c.get(f"/api/payhot/orders/{order_id}", headers=auth())
    assert r.status == 200
    assert (await r.json())["status"] == "succeeded"
    assert seen == [(order_id, fake)]


async def test_someone_elses_order_is_not_found(paying):
    c, _, _ = paying
    order_id = (await (await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"},
                                    headers=auth())).json())["order_id"]
    assert (await c.get(f"/api/payhot/orders/{order_id}", headers=auth(43))).status == 404
    assert (await c.get("/api/payhot/orders/not-a-uuid", headers=auth())).status == 404


async def test_a_payhot_refusal_is_a_clear_error(paying):
    from src.payments.payhot import PayHotError

    c, fake, store = paying

    async def refuse(**k):
        raise PayHotError("payment_route_unavailable", "no route")

    fake.create_payment = refuse
    r = await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"}, headers=auth())
    assert r.status == 502
    assert "error" in await r.json()


async def test_the_background_poll_credits_and_tells_the_user(paying, monkeypatch):
    """Человек оплатил и закрыл приложение — токены всё равно придут, и бот скажет."""
    from types import SimpleNamespace

    from src.db import repo
    from src.payments import orders
    from src.payments.orders import Settled
    from src.webapp import server as mod

    c, fake, store = paying
    app = c.server.app
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    app[mod.BOT_KEY].send_message = send_message
    from datetime import datetime, timezone

    o1, o2, o3 = (SimpleNamespace(id=f"o{i}", created_at=datetime.now(timezone.utc)) for i in range(3))

    async def to_check(*, since):
        return [o1, o2, o3]

    async def settle(oid, client):
        if oid == "o2":
            raise RuntimeError("битый заказ")
        if oid == "o1":
            return Settled("succeeded", True, True, 240, 42)
        return Settled("processing", False, False, 240, 42)

    monkeypatch.setattr(repo, "payhot_orders_to_check", to_check)
    monkeypatch.setattr(orders, "settle", settle)
    assert await mod.payhot_poll_once(app) == 1
    assert len(sent) == 1 and sent[0][0] == 42 and "240" in sent[0][1]


async def test_too_many_orders_in_a_row_are_refused(paying, monkeypatch):
    """Каждый заказ — настоящий счёт в PayHot и работа для фонового опроса."""
    from src.db import repo

    c, fake, store = paying

    async def count(user_id, *, since):
        return len(store["orders"])

    monkeypatch.setattr(repo, "count_payhot_orders", count)
    for _ in range(5):
        assert (await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"},
                             headers=auth())).status == 200
    assert (await c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"},
                         headers=auth())).status == 429
    assert len(fake.created) == 5


def test_an_http_return_url_is_a_config_error():
    with pytest.raises(ValueError):
        Settings(_env_file=None, payhot_return_url="http://example.com")


async def test_old_orders_are_polled_rarely_not_never(paying, monkeypatch):
    """Свежие — каждый проход; старше часа — раз в несколько проходов, но до
    недели: оплата, подтверждённая поздно или пока бот лежал, всё равно дойдёт."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from src.db import repo
    from src.payments import orders
    from src.payments.orders import Settled
    from src.webapp import server as mod

    c, fake, store = paying
    now = datetime.now(timezone.utc)
    fresh = SimpleNamespace(id="fresh", created_at=now - timedelta(minutes=5))
    old = SimpleNamespace(id="old", created_at=now - timedelta(days=2))
    seen_since = []

    async def to_check(*, since):
        seen_since.append(since)
        return [fresh, old]

    asked = []

    async def settle(oid, client):
        asked.append(oid)
        return Settled("processing", False, False, 240, 42)

    monkeypatch.setattr(repo, "payhot_orders_to_check", to_check)
    monkeypatch.setattr(orders, "settle", settle)
    for n in range(mod.PAYHOT_SLOW_EVERY):
        await mod.payhot_poll_once(c.server.app, pass_no=n)
    assert asked.count("fresh") == mod.PAYHOT_SLOW_EVERY
    assert asked.count("old") == 1
    assert now - seen_since[0] >= timedelta(days=6), "окно опроса короче недели"


async def test_orders_sent_all_at_once_still_respect_the_limit(paying, monkeypatch):
    """Проверка лимита и создание счёта разделены походом в PayHot. Десять
    запросов разом не должны все пройти проверку до первой записи."""
    import asyncio

    from src.db import repo

    c, fake, store = paying

    async def count(user_id, *, since):
        n = len(store["orders"])
        await asyncio.sleep(0.01)  # как настоящая база: пока считаем, приходят соседние запросы
        return n

    real_create = fake.create_payment

    async def slow_create(**k):
        await asyncio.sleep(0.05)
        return await real_create(**k)

    monkeypatch.setattr(repo, "count_payhot_orders", count)
    fake.create_payment = slow_create
    got = await asyncio.gather(*(
        c.post("/api/payhot/orders", json={"tokens": 240, "method": "crypto"}, headers=auth())
        for _ in range(10)
    ))
    assert sorted(r.status for r in got) == [200] * 5 + [429] * 5
    assert len(fake.created) == 5


# ------------------------------------------------------------ музыка из рилса

REEL_URL = "https://www.instagram.com/reel/DfXkZV0Sbuu/?igsh=x"


@pytest.fixture
def reel_fetch(monkeypatch):
    """Instagram без сети: «скачанный» трек — пара байт в папке приёма."""
    from src.pipeline import reel_audio
    from src.pipeline.reel_audio import ReelAudioError, ReelTrack

    from types import SimpleNamespace

    from src.db import repo

    seen = {"urls": [], "fail": None, "extractors": [], "files": []}

    async def upsert_user(user_id, **k):
        return SimpleNamespace(id=user_id, is_blocked=False, is_admin=False, balance=3, lang="ru",
                               language_code="ru", interface_lang=None)

    monkeypatch.setattr(repo, "upsert_user", upsert_user)

    async def fake_fetch(url, dst_dir, extractors="Instagram"):
        seen["urls"].append(url)
        seen["extractors"].append(extractors)
        if seen["fail"]:
            raise ReelAudioError(seen["fail"])
        path = Path(dst_dir) / "track.m4a"
        path.write_bytes(b"m4a!")
        return ReelTrack(path=path, duration=36.155)

    async def fake_file(url, dst_dir, client=None):
        seen["files"].append(url)
        path = Path(dst_dir) / "track.bin"
        path.write_bytes(b"mp3!")
        return ReelTrack(path=path, duration=12.0)

    monkeypatch.setattr(reel_audio, "fetch_reel_audio", fake_fetch)
    monkeypatch.setattr(reel_audio, "fetch_file_audio", fake_file)
    return seen


async def _music(client, url=REEL_URL, user_id=42):
    return await client.post("/api/music", json={"url": url}, headers=auth(user_id))


async def test_a_reel_link_gives_a_music_id(client, reel_fetch):
    from src.webapp import server as mod

    r = await _music(client)
    assert r.status == 200
    body = await r.json()
    assert body["duration"] == pytest.approx(36.155)
    folder = mod._music_dir(client.server.app[mod.CFG_KEY], body["music_id"])
    assert (folder / "track.m4a").read_bytes() == b"m4a!"
    assert json.loads((folder / "meta.json").read_text())["user_id"] == 42
    assert reel_fetch["urls"] == ["https://www.instagram.com/reel/DfXkZV0Sbuu/"]


@pytest.mark.parametrize("url", ["ftp://example.com/a.mp3", "file:///etc/passwd", "https://example.com"])
async def test_a_link_we_cannot_use_is_refused_without_downloading(client, reel_fetch, url):
    """Любая http(s)-ссылка на файл теперь допустима (её качает свой загрузчик с
    проверкой адресов), а не-http и ссылки без пути — нет."""
    r = await _music(client, url)
    assert r.status == 400
    assert reel_fetch["urls"] == [] and reel_fetch["files"] == []


@pytest.mark.parametrize("code, status", [
    ("no_audio", 422), ("unavailable", 422), ("instagram_down", 502), ("too_big", 413),
])
async def test_each_reel_failure_has_its_own_message(client, reel_fetch, code, status):
    from src.core.i18n import t
    from src.webapp import server as mod

    reel_fetch["fail"] = code
    r = await _music(client)
    assert r.status == status
    assert (await r.json())["error"] == t(f"music.{code}", "ru")
    assert not list(Path(client.server.app[mod.CFG_KEY].work_dir).glob("music-*"))


async def test_the_seventh_check_in_a_minute_is_refused(client, reel_fetch):
    for _ in range(6):
        assert (await _music(client)).status == 200
    assert (await _music(client)).status == 429
    assert (await _music(client, user_id=43)).status == 200, "лимит одного задел другого"


@pytest.fixture
def finish_ok(client, monkeypatch):
    """`finish`, который доходит до очереди: задача создаётся, файлы «заливаются»."""
    import uuid
    from types import SimpleNamespace

    from src.db import repo
    from src.db.models import Job, JobStatus
    from src.pipeline import probe as probe_mod
    from src.webapp import server as mod
    from src.worker import tasks

    seen = {"uploads": {}, "updates": {}, "enqueued": 0}
    job = Job(id=uuid.uuid4(), user_id=42, status=JobStatus.QUEUED, preset="glass", options={},
              progress=0.0, delivered=False)

    async def upsert_user(*a, **k):
        return SimpleNamespace(id=42, is_blocked=False, is_admin=False, balance=3, lang="ru",
                               language_code="ru", interface_lang=None)

    async def zero(*a, **k):
        return 0

    async def create_job(*a, **k):
        return job

    async def update_job(job_id, **k):
        seen["updates"].update(k)

    async def noop(*a, **k):
        return None

    async def get_job(job_id):
        return job

    async def upload(path, key, **k):
        seen["uploads"][key] = Path(path).read_bytes()
        return key

    class Queue:
        async def enqueue_job(self, *a, **k):
            seen["enqueued"] += 1

        async def close(self):
            return None

    async def get_queue(*a, **k):
        return Queue()

    monkeypatch.setattr(repo, "upsert_user", upsert_user)
    monkeypatch.setattr(repo, "active_jobs_count", zero)
    monkeypatch.setattr(repo, "create_job", create_job)
    monkeypatch.setattr(repo, "update_job", update_job)
    monkeypatch.setattr(repo, "set_user_preset", noop)
    monkeypatch.setattr(repo, "set_user_settings", noop)
    monkeypatch.setattr(repo, "get_job", get_job)
    monkeypatch.setattr(mod.storage, "upload", upload)
    monkeypatch.setattr(tasks, "get_queue", get_queue)
    monkeypatch.setattr(probe_mod, "probe", lambda p: SimpleNamespace(duration=20.0, has_audio=True))
    return seen


async def test_finish_attaches_the_music_to_the_job(client, reel_fetch, finish_ok):
    from src.webapp import server as mod

    music_id = (await (await _music(client)).json())["music_id"]
    bottom = await _uploaded(client, b"abcd")
    r = await client.post(f"/api/uploads/{bottom}/finish",
                          json={"preset": "glass", "options": {}, "music_id": music_id},
                          headers=auth())
    assert r.status == 200
    body = await r.json()
    assert not body.get("music_dropped")
    key = finish_ok["updates"]["music_key"]
    assert key.endswith("/music.m4a")
    assert finish_ok["uploads"][key] == b"m4a!"
    assert not mod._music_dir(client.server.app[mod.CFG_KEY], music_id).exists()


@pytest.mark.parametrize("which", ["foreign", "missing", "junk"])
async def test_a_bad_music_id_only_drops_the_music(client, reel_fetch, finish_ok, which):
    """Монтаж запущен, пока трек ещё проверялся, или папку трека уже убрали:
    видео не перезаливается, ролик просто без музыки."""
    music_id = {
        "foreign": (await (await _music(client, user_id=43)).json())["music_id"],
        "missing": "0" * 32,
        "junk": "../x",
    }[which]
    bottom = await _uploaded(client, b"abcd")
    r = await client.post(f"/api/uploads/{bottom}/finish",
                          json={"preset": "glass", "options": {}, "music_id": music_id},
                          headers=auth())
    assert r.status == 200
    assert (await r.json())["music_dropped"] is True
    assert "music_key" not in finish_ok["updates"]
    assert finish_ok["enqueued"] == 1


async def test_a_music_upload_failure_costs_only_the_music(client, reel_fetch, finish_ok, monkeypatch):
    from src.webapp import server as mod

    real = mod.storage.upload

    async def flaky(path, key, **k):
        if key.endswith("music.m4a"):
            raise RuntimeError("хранилище легло")
        return await real(path, key, **k)

    monkeypatch.setattr(mod.storage, "upload", flaky)
    music_id = (await (await _music(client)).json())["music_id"]
    bottom = await _uploaded(client, b"abcd")
    r = await client.post(f"/api/uploads/{bottom}/finish",
                          json={"preset": "glass", "options": {}, "music_id": music_id},
                          headers=auth())
    assert r.status == 200
    assert (await r.json())["music_dropped"] is True
    assert finish_ok["enqueued"] == 1


async def test_an_abandoned_music_folder_is_swept_like_an_upload(tmp_path, monkeypatch):
    import os
    import time as _time

    from src.worker import tasks

    monkeypatch.setattr(tasks.settings, "work_dir", tmp_path)
    old_dir, fresh_dir = tmp_path / "music-old", tmp_path / "music-fresh"
    for d, age in ((old_dir, 3 * 3600), (fresh_dir, 3600)):
        d.mkdir()
        (d / "track.m4a").write_bytes(b"x")
        t = _time.time() - age
        os.utime(d, (t, t))
    await tasks.cleanup_old_files({})
    assert not old_dir.exists(), "брошенный трек пролежал бы сутки"
    assert fresh_dir.exists()


async def test_a_new_track_replaces_the_users_previous_one(client, reel_fetch):
    """Шесть проверок в минуту по 25 МБ за два часа до уборки — гигабайты чужих
    треков. У человека живёт только последний: прежние ему уже не нужны."""
    from src.webapp import server as mod

    cfg = client.server.app[mod.CFG_KEY]
    first = (await (await _music(client)).json())["music_id"]
    other = (await (await _music(client, user_id=43)).json())["music_id"]
    second = (await (await _music(client)).json())["music_id"]
    assert not mod._music_dir(cfg, first).exists()
    assert mod._music_dir(cfg, second).exists()
    assert mod._music_dir(cfg, other).exists(), "снесли трек другого человека"


async def test_a_blocked_user_cannot_check_links(client, reel_fetch, monkeypatch):
    from types import SimpleNamespace

    from src.db import repo

    async def blocked(*a, **k):
        return SimpleNamespace(id=42, is_blocked=True, is_admin=False, balance=0, lang="ru",
                               language_code="ru", interface_lang=None)

    monkeypatch.setattr(repo, "upsert_user", blocked)
    r = await _music(client)
    assert r.status == 403
    assert reel_fetch["urls"] == []


async def test_no_track_is_fetched_onto_a_full_disk(client, reel_fetch, monkeypatch):
    import shutil as _shutil
    from collections import namedtuple

    from src.webapp import server as mod

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(_shutil, "disk_usage", lambda p: usage(1, 1, mod.DISK_RESERVE_MB * 1024 * 1024))
    r = await _music(client)
    assert r.status == 507
    assert reel_fetch["urls"] == []


async def test_a_slow_earlier_check_does_not_delete_the_newer_track(client, reel_fetch, monkeypatch):
    """Ссылку A заменили на B; B скачалась первой и показана «Трек ✓», A —
    позже. A не имеет права снести B: в `finish` уйдёт именно B."""
    import asyncio

    from src.pipeline import reel_audio
    from src.pipeline.reel_audio import ReelTrack
    from src.webapp import server as mod

    release_a = asyncio.Event()

    async def fake_fetch(url, dst_dir, extractors="Instagram"):
        if "AAAAAAA" in url:
            await release_a.wait()
        path = Path(dst_dir) / "track.m4a"
        path.write_bytes(b"m4a!")
        return ReelTrack(path=path, duration=10.0)

    monkeypatch.setattr(reel_audio, "fetch_reel_audio", fake_fetch)
    slow = asyncio.ensure_future(_music(client, "https://www.instagram.com/reel/AAAAAAA/"))
    await asyncio.sleep(0.05)
    b = (await (await _music(client, "https://www.instagram.com/reel/BBBBBBB/")).json())["music_id"]
    release_a.set()
    assert (await slow).status == 200
    assert mod._music_dir(client.server.app[mod.CFG_KEY], b).exists(), "медленная A снесла свежую B"


async def test_typing_a_link_does_not_use_up_the_limit(client, reel_fetch):
    """Недописанная ссылка — не поход в Instagram и в лимит не идёт."""
    for partial in ["h", "https://www.insta", "https://www.instagram.com/re", "x", "y", "z", "w"]:
        assert (await _music(client, partial)).status == 400
    assert (await _music(client)).status == 200



async def test_a_tiktok_link_runs_the_tiktok_extractors(client, reel_fetch):
    r = await _music(client, "https://www.tiktok.com/@audible/video/7537416115673173278?lang=ru")
    assert r.status == 200
    assert reel_fetch["urls"] == ["https://www.tiktok.com/@audible/video/7537416115673173278"]
    assert reel_fetch["extractors"] == ["TikTok,vm.tiktok"]


async def test_a_direct_file_link_is_fetched_by_our_own_downloader(client, reel_fetch):
    r = await _music(client, "https://cdn.example.com/track.mp3")
    assert r.status == 200 and (await r.json())["duration"] == 12.0
    assert reel_fetch["files"] == ["https://cdn.example.com/track.mp3"] and reel_fetch["urls"] == []


async def test_youtube_is_declined_with_a_clear_message(client, reel_fetch):
    from src.core.i18n import t

    r = await _music(client, "https://youtu.be/jNQXAC9IVRw")
    assert r.status == 422
    assert (await r.json())["error"] == t("music.unsupported", "ru")
    assert reel_fetch["urls"] == [] and reel_fetch["files"] == []


async def test_a_new_job_never_takes_a_font_from_the_request(client, finish_ok, monkeypatch):
    """Шрифт, присланный приложением (старая версия в кеше Telegram), не доезжает
    до задачи и стирается из профиля: убрать его человеку уже нечем."""
    from src.db import repo

    seen: dict = {}
    create = repo.create_job

    async def create_job(*a, **k):
        seen["options"] = k.get("options")
        return await create(*a, **k)

    async def set_user_settings(user_id, **k):
        seen["settings"] = k

    monkeypatch.setattr(repo, "create_job", create_job)
    monkeypatch.setattr(repo, "set_user_settings", set_user_settings)
    bottom = await _uploaded(client, b"abcd")
    r = await client.post(
        f"/api/uploads/{bottom}/finish",
        json={"preset": "glass", "options": {"font": "serif", "accent": "red"}},
        headers=auth(),
    )
    assert r.status == 200
    assert seen["options"]["font"] == "theme"
    assert seen["options"]["accent"] == "red", "цвет при этом остаётся выбором человека"
    assert {k: seen["settings"][k] for k in ("accent", "font")} == {"accent": "red", "font": "theme"}


async def test_the_effect_switches_are_remembered_between_reels(client, finish_ok, monkeypatch):
    """Переключатели эффектов пишутся в профиль вместе с цветом: их одиннадцать,
    и выставлять «свои» перед каждым роликом заново никто не станет. Нарезка не
    запоминается — её выбирают под конкретное видео."""
    from src.core.options import REMEMBERED
    from src.db import repo

    seen: dict = {}
    create = repo.create_job

    async def create_job(*a, **k):
        seen["options"] = k.get("options")
        return await create(*a, **k)

    async def set_user_settings(user_id, **k):
        seen["settings"] = k

    monkeypatch.setattr(repo, "create_job", create_job)
    monkeypatch.setattr(repo, "set_user_settings", set_user_settings)
    bottom = await _uploaded(client, b"abcd")
    r = await client.post(
        f"/api/uploads/{bottom}/finish",
        json={"preset": "glass", "options": {"studio": False, "tracking": False, "cut": False}},
        headers=auth(),
    )
    assert r.status == 200
    assert seen["options"]["studio"] is False and seen["options"]["tracking"] is False
    assert seen["options"]["smooth"] is True, "чего в запросе нет — включено"
    assert set(seen["settings"]) == {"accent", "font", *REMEMBERED}
    assert seen["settings"]["studio"] is False and seen["settings"]["tracking"] is False
    assert "cut" not in seen["settings"]


# ------------------------------------------------------------ вид приложения 27.11.2026


JOB_ID = "11111111-1111-1111-1111-111111111111"


def _done_job(**extra):
    """Готовый ролик, каким его отдаёт база: 24 ноября 00:38."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from src.db.models import JobStatus

    born = datetime(2026, 11, 24, 0, 38, tzinfo=timezone.utc)
    fields = dict(
        id=JOB_ID, user_id=42, status=JobStatus.DONE, stage=None, progress=1.0, preset="glass",
        options={}, created_at=born, finished_at=born, source_duration=60.0, result_duration=40.0,
        error=None, error_code=None, delivered=True, result_key="results/x/result.mp4",
        files_purged_at=None,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_a_finished_reel_carries_a_thumbnail_and_its_storage_deadline():
    """Строка списка: миниатюра и «хранится до 24 дек.» — срок от окончания
    монтажа, тот же, по которому файлы убирает воркер."""
    from src.webapp.server import _job_view

    cfg = Settings(_env_file=None, bot_token=TOKEN, storage_retention_days=30)
    view = _job_view(_done_job(), live=None, secret=TOKEN, lang="ru", cfg=cfg)
    path, token = view["thumb_url"].split("?t=")
    assert path == f"/media/{JOB_ID}.jpg"
    assert check_media_token(JOB_ID, token, TOKEN), "миниатюра ходит по той же подписи, что ролик"
    assert view["expires_at"].startswith("2026-12-24")


def test_a_reel_without_files_promises_neither():
    """Файлы убраны по сроку — ни кадра, ни даты; срок хранения выключен — даты нет."""
    from datetime import datetime, timezone

    from src.db.models import JobStatus
    from src.webapp.server import _job_view

    cfg = Settings(_env_file=None, bot_token=TOKEN, storage_retention_days=30)
    purged = _done_job(files_purged_at=datetime(2026, 12, 24, tzinfo=timezone.utc))
    view = _job_view(purged, live=None, secret=TOKEN, lang="ru", cfg=cfg)
    assert view["thumb_url"] is None and view["expires_at"] is None and view["purged"] is True

    running = _done_job(status=JobStatus.RUNNING, result_key=None, finished_at=None)
    view = _job_view(running, live=None, secret=TOKEN, lang="ru", cfg=cfg)
    assert view["thumb_url"] is None and view["expires_at"] is None

    forever = Settings(_env_file=None, bot_token=TOKEN, storage_retention_days=0)
    view = _job_view(_done_job(), live=None, secret=TOKEN, lang="ru", cfg=forever)
    assert view["thumb_url"] and view["expires_at"] is None


async def test_the_thumbnail_needs_a_signed_link(client):
    r = await client.get(f"/media/{JOB_ID}.jpg?t=garbage")
    assert r.status == 403


async def test_the_thumbnail_is_a_frame_of_the_reel(client, monkeypatch, tmp_path):
    """Кадр снимается ffmpeg-ом и кладётся в кеш; сам ролик ради кадра в кеше
    не остаётся — список из тридцати роликов иначе удвоил бы их место на диске."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("нужен ffmpeg")
    from src.db import repo
    from src.webapp import server as mod

    sample = tmp_path / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=30",
         "-t", "1", "-pix_fmt", "yuv420p", str(sample)],
        check=True,
    )
    calls = []

    async def get_job(job_id):
        return _done_job() if str(job_id) == JOB_ID else None

    async def download(key, path):
        calls.append(key)
        shutil.copyfile(sample, path)
        return Path(path)

    monkeypatch.setattr(repo, "get_job", get_job)
    monkeypatch.setattr(mod.storage, "download", download)
    url = f"/media/{JOB_ID}.jpg?t={media_token(JOB_ID, TOKEN)}"

    r = await client.get(url)
    assert r.status == 200 and r.content_type == "image/jpeg"
    assert (await r.read())[:2] == b"\xff\xd8", "это JPEG"
    cache = tmp_path / "media-cache"
    assert sorted(f.name for f in cache.iterdir()) == [f"{JOB_ID}.jpg"], "ни ролика, ни недописанных файлов"

    assert (await client.get(url)).status == 200
    assert calls == ["results/x/result.mp4"], "второй запрос отдаёт готовый кадр из кеша"

    purged = await client.get(f"/media/22222222-2222-2222-2222-222222222222.jpg?t="
                              f"{media_token('22222222-2222-2222-2222-222222222222', TOKEN)}")
    assert purged.status == 404


async def test_bootstrap_carries_the_profile_links_and_the_partner_share(booted):
    """Строки профиля «Официальный канал», «Поддержка», «Публичная оферта» ведут
    по ссылкам с сервера; ненастроенная ссылка — пустая, и строки в приложении нет."""
    body = await (await booted.get("/api/bootstrap", headers=auth())).json()
    assert set(body["links"]) == {"channel", "support", "offer"}
    assert body["links"]["channel"] == "" and body["links"]["offer"] == ""
    assert body["partner_percent"] == 15


def test_a_support_contact_becomes_a_telegram_link():
    from src.webapp.server import _tg_link

    assert _tg_link("@reel_support") == "https://t.me/reel_support"
    assert _tg_link("https://t.me/reel_support") == "https://t.me/reel_support"
    assert _tg_link("напишите нам") == "", "из произвольного текста ссылку не выдумываем"
    assert _tg_link(None) == "" and _tg_link("  ") == ""
