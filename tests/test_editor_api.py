"""Редактор сцен, сторона сервера: сцены ролика и «Собрать» (новая версия).

База, очередь и бот — двойники: проверяется, что именно сервер списывает, что
кладёт в новую задачу и что отказывает до списания. Сами правки листа —
`tests/test_edit.py`.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.core.config import Settings
from src.db.models import JobStatus
from src.webapp.auth import sign_init_data
from tests.test_edl import base_edl

TOKEN = "123456:TEST-token"
JOB_ID = "11111111-1111-1111-1111-111111111111"
ROOT_ID = "22222222-2222-2222-2222-222222222222"


def auth(user_id: int = 42) -> dict[str, str]:
    data = sign_init_data(
        {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Т"})}, TOKEN
    )
    return {"Authorization": f"tma {data}"}


def done_job(**extra):
    edl = base_edl()
    fields = dict(
        id=uuid.UUID(JOB_ID), user_id=42, status=JobStatus.DONE, stage=None, progress=1.0, preset="glass",
        options={"cut": True}, created_at=datetime(2026, 11, 24, tzinfo=timezone.utc),
        finished_at=datetime(2026, 11, 24, tzinfo=timezone.utc), source_duration=5.0, result_duration=4.9,
        error=None, error_code=None, delivered=True, result_key="results/x/result.mp4",
        files_purged_at=None, edl=json.loads(edl.model_dump_json()), kind="video", root_id=None, version=1,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


@pytest.fixture
async def stand(tmp_path, monkeypatch):
    """Клиент и журнал того, что сервер сделал с базой, очередью и чатом."""
    from src.bot.handlers import video
    from src.db import repo
    from src.webapp.server import create_app
    from src.worker import tasks

    log = SimpleNamespace(created=[], queued=[], said=[], watched=[], refunded=[], balance=100, jobs={},
                          no_balance=False)
    log.jobs[JOB_ID] = done_job()

    async def upsert(user_id, **fields):
        return SimpleNamespace(id=user_id, balance=log.balance, videos_done=1, preset="glass", settings={},
                               lang="ru", ref_credit=0, username=None, first_name="Т", language_code="ru",
                               is_admin=fields.get("is_admin", False))

    async def get_job(job_id):
        return log.jobs.get(str(job_id))

    async def next_version(root_id):
        return 3

    async def create_job(user_id, **kw):
        if log.no_balance:
            raise repo.InsufficientBalance(user_id)
        log.created.append(kw)
        made = done_job(id=uuid.uuid4(), status=JobStatus.QUEUED, result_key=None, version=kw.get("version", 1),
                        root_id=kw.get("root_id"), edl=kw.get("edl"), options=kw["options"], preset=kw["preset"],
                        kind=kw.get("kind", "video"))
        log.jobs[str(made.id)] = made
        return made

    async def nothing(*a, **k):
        return None

    async def refund(job_id):
        log.refunded.append(str(job_id))
        return True

    class Queue:
        async def enqueue_job(self, name, job_id, **kw):
            log.queued.append((name, job_id))

        async def close(self):
            pass

    async def get_queue(*a, **k):
        return Queue()

    async def send_message(chat_id, text, **kw):
        log.said.append(text)
        return SimpleNamespace(message_id=7)

    async def me():
        return SimpleNamespace(username="bot", first_name="Бот")

    monkeypatch.setattr(repo, "upsert_user", upsert)
    monkeypatch.setattr(repo, "get_job", get_job)
    monkeypatch.setattr(repo, "next_version", next_version)
    monkeypatch.setattr(repo, "create_job", create_job)
    monkeypatch.setattr(repo, "update_job", nothing)
    monkeypatch.setattr(repo, "mark_failed", nothing)
    monkeypatch.setattr(repo, "refund_job", refund)
    monkeypatch.setattr(tasks, "get_queue", get_queue)
    monkeypatch.setattr(video, "watch_job", lambda *a: log.watched.append(a[3]))

    cfg = Settings(_env_file=None, bot_token=TOKEN, work_dir=tmp_path, tokens_per_edit=4, free_videos=False)
    app = create_app(bot=SimpleNamespace(me=me, send_message=send_message), cfg=cfg)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        yield client, log


async def test_the_editor_gets_the_scenes_of_a_finished_reel(stand):
    client, _ = stand
    r = await client.get(f"/api/jobs/{JOB_ID}/scenes", headers=auth())
    assert r.status == 200
    body = await r.json()
    assert [s["text"] for s in body["scenes"]] == ["w0 w1 w2", "w3 w4 w5", "w6 w7 w8"]
    assert body["cost"] == 4 and body["preset"] == "glass" and body["job"]["editable"] is True
    assert body["job"]["version"] == 1 and body["job"]["kind"] == "video"


async def test_somebody_elses_reel_is_not_found(stand):
    client, _ = stand
    r = await client.get(f"/api/jobs/{JOB_ID}/scenes", headers=auth(user_id=77))
    assert r.status == 404


async def test_a_reel_without_a_saved_edit_cannot_be_opened(stand):
    client, log = stand
    log.jobs[JOB_ID] = done_job(edl=None)
    assert (await client.get(f"/api/jobs/{JOB_ID}/scenes", headers=auth())).status == 409
    log.jobs[JOB_ID] = done_job(status=JobStatus.RUNNING)
    assert (await client.get(f"/api/jobs/{JOB_ID}/scenes", headers=auth())).status == 409


async def test_a_version_whose_source_is_gone_says_so_before_any_editing(stand):
    """Исходник версии лежит у исходной задачи; удалён по сроку — собирать не из чего."""
    client, log = stand
    log.jobs[JOB_ID] = done_job(root_id=uuid.UUID(ROOT_ID), version=2)
    log.jobs[ROOT_ID] = done_job(id=uuid.UUID(ROOT_ID), files_purged_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert (await client.get(f"/api/jobs/{JOB_ID}/scenes", headers=auth())).status == 410
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {"scenes": {"0": {"text": "а б в"}}}},
                          headers=auth())
    assert r.status == 410 and log.created == []


async def test_building_a_version_creates_a_charged_job_from_the_edited_list(stand):
    client, log = stand
    edits = {"scenes": {"0": {"text": "Эта тачка лям", "size": "l"}, "1": {"cut": [4]}}}
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": edits}, headers=auth())
    assert r.status == 200, await r.text()
    made = log.created[0]
    # Версия ссылается на исходную задачу, своих файлов исходника у неё нет.
    assert str(made["root_id"]) == JOB_ID and made["version"] == 3
    assert made["source_key"] is None and made["source_duration"] == 5.0
    assert made["charge"] is True and made["cost"] == 4
    assert [w["text"] for w in made["edl"]["words"][:3]] == ["Эта", "тачка", "лям"]
    assert made["edl"]["captions"][0]["size"] == "l"
    assert [cw["word_idx"] for cw in made["edl"]["captions"][1]["words"]] == [3, 5]
    assert made["options"]["cut"] is True
    # В работу версия идёт той же задачей воркера, что и ролик, и за ней следит чат.
    assert [name for name, _ in log.queued] == ["process_job"]
    assert log.said == ["Собираю версию 3…"] and len(log.watched) == 1
    body = await r.json()
    assert body["job"]["version"] == 3 and body["job"]["status"] == "queued"


async def test_a_version_of_a_version_keeps_the_original_root(stand):
    client, log = stand
    log.jobs[JOB_ID] = done_job(root_id=uuid.UUID(ROOT_ID), version=2)
    log.jobs[ROOT_ID] = done_job(id=uuid.UUID(ROOT_ID))
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {"scenes": {"0": {"position": "top"}}}},
                          headers=auth())
    assert r.status == 200
    assert str(log.created[0]["root_id"]) == ROOT_ID


async def test_changing_only_the_style_is_a_version_too(stand):
    client, log = stand
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {}, "preset": "bold"}, headers=auth())
    assert r.status == 200
    assert log.created[0]["preset"] == "bold" and log.created[0]["edl"]["preset"] == "bold"


async def test_nothing_changed_costs_nothing(stand):
    client, log = stand
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {"scenes": {}}}, headers=auth())
    assert r.status == 400 and log.created == [] and log.queued == []
    # Неизвестный стиль — не правка.
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {}, "preset": "нет-такого"}, headers=auth())
    assert r.status == 400 and log.created == []


async def test_bad_edits_are_refused_before_charging(stand):
    client, log = stand
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {"scenes": {"0": {"cut": [8]}}}},
                          headers=auth())
    assert r.status == 400 and log.created == []


async def test_a_picture_must_be_the_users_own_upload(stand):
    client, log = stand
    foreign = "edit-media/2026/11/27/77/" + "a" * 32 + ".jpg"
    r = await client.post(
        f"/api/jobs/{JOB_ID}/edit",
        json={"edits": {"scenes": {"0": {"media": {"op": "set", "key": foreign}}}}}, headers=auth(),
    )
    assert r.status == 400 and log.created == []
    own = "edit-media/2026/11/27/42/" + "a" * 32 + ".jpg"
    r = await client.post(
        f"/api/jobs/{JOB_ID}/edit",
        json={"edits": {"scenes": {"0": {"media": {"op": "set", "key": own}}}}}, headers=auth(),
    )
    assert r.status == 200
    assert log.created[0]["edl"]["broll"][0]["asset_key"] == own


async def test_no_tokens_no_version(stand):
    client, log = stand
    log.no_balance = True
    r = await client.post(f"/api/jobs/{JOB_ID}/edit", json={"edits": {"scenes": {"0": {"size": "s"}}}},
                          headers=auth())
    assert r.status == 402 and log.queued == [] and log.said == []


async def test_an_uploaded_picture_becomes_a_jpeg_under_the_users_key(stand, monkeypatch):
    import io

    from PIL import Image

    from src.webapp import server as mod

    client, _ = stand
    stored = {}

    async def upload(path, key, **kw):
        stored[key] = open(path, "rb").read()
        return key

    monkeypatch.setattr(mod.storage, "upload", upload)
    buf = io.BytesIO()
    Image.new("RGB", (3000, 1500), (200, 80, 40)).save(buf, "PNG")
    r = await client.post("/api/edit-media", data=buf.getvalue(), headers=auth())
    assert r.status == 200, await r.text()
    body = await r.json()
    assert mod._own_media_key(42, body["key"]) and body["aspect"] == 2.0
    with Image.open(io.BytesIO(stored[body["key"]])) as img:
        assert img.format == "JPEG" and max(img.size) == 1600

    r = await client.post("/api/edit-media", data=b"not a picture", headers=auth())
    assert r.status == 415


# ------------------------------------------------------------------ обложки


async def test_ordering_a_cover_creates_a_charged_cover_job(stand):
    client, log = stand
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "neon", "text": "  Топ 3   ошибки "},
                          headers=auth())
    assert r.status == 200, await r.text()
    made = log.created[0]
    assert made["kind"] == "cover" and str(made["root_id"]) == JOB_ID
    assert made["charge"] is True and made["cost"] == 5
    assert made["options"] == {"cover": {"style": "neon", "text": "Топ 3 ошибки", "photo_key": None}}
    assert [name for name, _ in log.queued] == ["process_job"] and log.said == ["Рисую обложку…"]


async def test_a_cover_of_a_version_takes_the_frame_from_the_original_source(stand):
    """У версии из редактора своих файлов нет: кадр берётся из исходной задачи."""
    client, log = stand
    log.jobs[JOB_ID] = done_job(root_id=uuid.UUID(ROOT_ID), version=2)
    log.jobs[ROOT_ID] = done_job(id=uuid.UUID(ROOT_ID))
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "plate", "text": ""}, headers=auth())
    assert r.status == 200 and str(log.created[0]["root_id"]) == ROOT_ID


async def test_an_unknown_style_falls_back_and_a_long_caption_is_cut(stand):
    from src.pipeline.cover import MAX_TEXT, style_keys

    client, log = stand
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "нет", "text": "я" * 300}, headers=auth())
    assert r.status == 200
    spec = log.created[0]["options"]["cover"]
    assert spec["style"] == style_keys()[0] and len(spec["text"]) == MAX_TEXT


async def test_a_cover_needs_the_source_or_an_own_photo(stand):
    client, log = stand
    log.jobs[JOB_ID] = done_job(files_purged_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "plate", "text": "а"}, headers=auth())
    assert r.status == 410 and log.created == []
    # Со своим фото исходник не нужен; чужое фото не подставить.
    own = "edit-media/2026/11/27/42/" + "b" * 32 + ".jpg"
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "plate", "text": "а", "photo_key": own},
                          headers=auth())
    assert r.status == 200 and log.created[0]["options"]["cover"]["photo_key"] == own
    foreign = own.replace("/42/", "/77/")
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "plate", "text": "а", "photo_key": foreign},
                          headers=auth())
    assert r.status == 400 and len(log.created) == 1


async def test_a_cover_is_not_made_for_somebody_elses_or_unfinished_reel(stand):
    client, log = stand
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "plate"}, headers=auth(user_id=77))
    assert r.status == 404
    log.jobs[JOB_ID] = done_job(status=JobStatus.RUNNING)
    r = await client.post("/api/covers", json={"job_id": JOB_ID, "style": "plate"}, headers=auth())
    assert r.status == 404 and log.created == []


async def test_bootstrap_lists_the_cover_styles_with_previews(stand):
    from src.pipeline.cover import style_keys
    from src.webapp.server import STATIC

    client, _ = stand
    body = await (await client.get("/api/bootstrap", headers=auth())).json()
    assert [s["key"] for s in body["cover"]["styles"]] == style_keys()
    assert body["cover"]["cost"] == 5 and body["cover"]["max_text"] == 80 and body["edit_cost"] == 4
    for style in body["cover"]["styles"]:
        assert (STATIC / "covers" / f"{style['key']}.jpg").is_file(), style["key"]


def test_a_cover_job_is_shown_as_a_picture_not_a_video():
    from src.webapp.server import _job_view

    cfg = Settings(_env_file=None, bot_token=TOKEN, storage_retention_days=30)
    job = done_job(kind="cover", edl=None, options={"cover": {"style": "neon", "text": "Привет"}},
                   result_key="results/x/cover.jpg")
    view = _job_view(job, live=None, secret=TOKEN, lang="ru", cfg=cfg)
    assert view["kind"] == "cover" and view["editable"] is False
    assert view["video_url"] is None and view["image_url"].startswith(f"/cover/{JOB_ID}.jpg?t=")
    assert view["thumb_url"] and view["cover"] == {"style": "neon", "text": "Привет"}
