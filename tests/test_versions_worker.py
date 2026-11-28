"""Воркер: версия ролика из редактора сцен и обложка.

Здесь то, что проверяется без рендера: откуда берётся исходник версии, как
доезжают свои картинки и как собирается обложка из фото. Сам рендер версии
проверяет прогон на сервере.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.core.edl import Broll, BrollAnim
from src.pipeline.job import JobError
from tests.test_edl import base_edl

ROOT = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _jpeg(size=(1200, 1600), color=(180, 120, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


async def test_an_ordinary_reel_is_its_own_source(monkeypatch):
    from src.worker import tasks

    job = SimpleNamespace(id=uuid.uuid4(), root_id=None)
    assert await tasks.source_job(job) is job
    # Задача из прошлой выкладки, у которой поля ещё нет вовсе.
    old = SimpleNamespace(id=uuid.uuid4())
    assert await tasks.source_job(old) is old


async def test_a_version_takes_the_files_of_the_original_job(monkeypatch):
    from src.db import repo
    from src.worker import tasks

    origin = SimpleNamespace(id=ROOT, files_purged_at=None, source_key="sources/x/source.mp4")

    async def get_job(job_id):
        return origin if uuid.UUID(str(job_id)) == ROOT else None

    monkeypatch.setattr(repo, "get_job", get_job)
    version = SimpleNamespace(id=uuid.uuid4(), root_id=ROOT)
    assert await tasks.source_job(version) is origin


async def test_a_version_of_a_purged_original_fails_with_a_clear_code(monkeypatch):
    from src.db import repo
    from src.worker import tasks

    async def get_job(job_id):
        return SimpleNamespace(id=ROOT, files_purged_at=datetime(2026, 12, 1, tzinfo=timezone.utc))

    monkeypatch.setattr(repo, "get_job", get_job)
    with pytest.raises(JobError) as err:
        await tasks.source_job(SimpleNamespace(id=uuid.uuid4(), root_id=ROOT))
    assert err.value.code == "source_lost"

    async def nothing(job_id):
        return None

    monkeypatch.setattr(repo, "get_job", nothing)
    with pytest.raises(JobError):
        await tasks.source_job(SimpleNamespace(id=uuid.uuid4(), root_id=ROOT))


async def test_own_pictures_are_fetched_and_dead_cache_entries_dropped(tmp_path):
    """Своя картинка скачивается и приводится к JPEG; нарисованная раньше
    остаётся, пока её файл цел; вставка без файла выпадает, а не роняет рендер."""
    from src.pipeline.reedit import MEDIA_SIDE, stage_pictures

    alive = tmp_path / "alive.png"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(alive)

    def pic(word, **kw):
        return Broll(word_idx=word, duration=2.0, query="q", anim=BrollAnim.POP_IN, anchor="card", **kw)

    edl = base_edl(broll=[
        pic(0, asset_key="edit-media/2026/11/27/42/" + "a" * 32 + ".jpg"),
        pic(3, asset=str(alive)),
        pic(6, asset=str(tmp_path / "gone.png")),
        pic(7),
    ])
    asked = []

    async def download(key, path):
        asked.append(key)
        Path(path).write_bytes(_jpeg((4000, 2000)))

    left = await stage_pictures(edl, tmp_path, download)
    assert left == 2 and [b.word_idx for b in edl.broll] == [0, 3]
    own = edl.broll[0]
    assert asked == [own.asset_key] and own.asset_aspect == 2.0
    with Image.open(own.asset) as img:
        assert img.format == "JPEG" and max(img.size) == MEDIA_SIDE
    assert edl.broll[1].asset == str(alive)
    assert not list(tmp_path.glob("*.src")), "сырой файл за собой убран"


async def test_a_picture_that_fails_to_download_is_dropped_not_fatal(tmp_path):
    from src.pipeline.reedit import stage_pictures

    edl = base_edl(broll=[Broll(word_idx=0, duration=2.0, query="q", anim=BrollAnim.POP_IN, anchor="card",
                                asset_key="edit-media/2026/11/27/42/" + "c" * 32 + ".jpg")])

    async def download(key, path):
        raise FileNotFoundError(key)

    assert await stage_pictures(edl, tmp_path, download) == 0 and edl.broll == []


async def test_a_cover_from_an_own_photo_is_composed_and_uploaded(tmp_path, monkeypatch):
    from src.worker import tasks

    stored = {}
    stages = []

    async def download(key, path):
        Path(path).write_bytes(_jpeg())

    async def upload(path, key, **kw):
        stored[key] = Path(path).read_bytes()
        return key

    async def publish(stage, share, key=None):
        stages.append(key)

    monkeypatch.setattr(tasks.storage, "download", download)
    monkeypatch.setattr(tasks.storage, "upload", upload)
    job = SimpleNamespace(
        id=uuid.uuid4(), root_id=ROOT, kind="cover",
        options={"cover": {"style": "plate", "text": "Топ 3 ошибки", "photo_key": "edit-media/x.jpg"}},
    )
    key = await tasks.build_cover(job, tmp_path, publish)
    assert key.endswith("/cover.jpg") and str(job.id) in key
    with Image.open(io.BytesIO(stored[key])) as img:
        assert img.format == "JPEG" and img.size == (1080, 1920)
    assert stages == ["download", "cover", "upload"]


async def test_a_cover_that_cannot_be_made_fails_with_its_own_code(tmp_path, monkeypatch):
    from src.worker import tasks

    async def download(key, path):
        Path(path).write_bytes(b"not a picture")

    async def publish(*a, **k):
        return None

    monkeypatch.setattr(tasks.storage, "download", download)
    job = SimpleNamespace(id=uuid.uuid4(), root_id=ROOT, kind="cover",
                          options={"cover": {"style": "plate", "text": "а", "photo_key": "edit-media/x.jpg"}})
    with pytest.raises(JobError) as err:
        await tasks.build_cover(job, tmp_path, publish)
    assert err.value.code == "cover_failed"


def test_the_version_caption_names_the_version():
    from src.bot.handlers.video import _caption

    job = SimpleNamespace(source_duration=60.0, result_duration=40.0, preset="glass", version=3,
                          options={}, music_key=None)
    assert _caption(job, "ru").startswith("Готова версия 3! ✨\n")
    first = SimpleNamespace(source_duration=60.0, result_duration=40.0, preset="glass", version=1,
                            options={}, music_key=None)
    assert "версия" not in _caption(first, "ru")


def test_the_result_keyboard_opens_the_scene_editor():
    from src.bot import keyboards

    was = keyboards.webapp_url()
    keyboards.set_webapp_url("https://app.example")
    try:
        kb = keyboards.result_keyboard("job-1", "glass", "ru")
        # Одна кнопка и ничего больше: кнопок стилей под роликом нет.
        assert [[b.text for b in row] for row in kb.inline_keyboard] == [["Редактировать"]]
        assert kb.inline_keyboard[0][0].web_app.url == "https://app.example/?screen=editor&job=job-1"
        keyboards.set_webapp_url(None)
        assert keyboards.result_keyboard("job-1", "glass", "ru") is None
    finally:
        keyboards.set_webapp_url(was)
