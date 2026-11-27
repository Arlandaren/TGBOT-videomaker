"""Фоновая музыка по смыслу ролика: библиотека, выбор трека, запись в лист."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.config import REPO_ROOT, Settings
from src.llm.client import LLMBadOutput, LLMUnavailable
from src.pipeline.audio_post import REEL_DUCK_DB
from src.pipeline.music import (
    CATALOG,
    Track,
    fallback_track,
    load_catalog,
    music_spec,
    pick_music,
    tracks_table,
)

TEXT = "Сегодня расскажу, как мы подняли продажи в три раза за один квартал."


def row(key: str, **kw) -> dict:
    return {
        "key": key, "file": f"{key}.m4a", "title": key.title(), "mood": "спокойный",
        "about": "теги автора: piano, calm", "energy": "low", "license": "cc0",
        "author": "someone", "source": f"https://example.org/{key}", **kw,
    }


def library(tmp_path: Path, rows: list[dict], *, files: bool = True) -> Settings:
    """Библиотека во временной папке: каталог и (пустые) файлы треков."""
    root = tmp_path / "music"
    root.mkdir(exist_ok=True)
    (root / CATALOG).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if files:
        for r in rows:
            if "file" in r:
                (root / r["file"]).write_bytes(b"\0")
    return Settings(_env_file=None, music_dir=root)


class FakeLLM:
    """Модель-двойник: отдаёт заготовленный ответ и помнит, что её спросили."""

    ready = True

    def __init__(self, answer=None, error: Exception | None = None):
        self.answer, self.error, self.calls = answer, error, []

    async def complete(self, name, variables, schema, tier, **kw):
        self.calls.append((name, variables, tier))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=self.answer)


# ------------------------------------------------------------------ библиотека


def test_only_tracks_that_need_no_credit_are_loaded(tmp_path):
    """CC BY под чужим рилсом без подписи автора — нарушение лицензии от имени
    нашего пользователя. Такая запись не загружается, как бы ни попала в каталог."""
    cfg = library(tmp_path, [
        row("calm-01"),
        row("calm-02", license="by"),
        row("calm-03", license="CC0"),
        row("own-01", license="own"),
        row("pd-01", license="pdm"),
        row("odd-01", license="by-nc"),
    ])
    assert [t.key for t in load_catalog(cfg)] == ["calm-01", "calm-03", "own-01", "pd-01"]


def test_a_broken_catalog_row_costs_only_itself(tmp_path):
    rows = [
        row("calm-01"),
        {"key": "нет полей"},
        row("calm-01", title="второй с тем же ключом"),
        row("Bad Key"),
        row("lost-01"),
    ]
    cfg = library(tmp_path, rows)
    (Path(cfg.music_dir) / "lost-01.m4a").unlink()
    got = load_catalog(cfg)
    assert [t.key for t in got] == ["calm-01"] and got[0].title == "Calm-01"


def test_no_catalog_means_no_music(tmp_path):
    cfg = Settings(_env_file=None, music_dir=tmp_path / "пусто")
    assert load_catalog(cfg) == []
    (tmp_path / "пусто").mkdir()
    (tmp_path / "пусто" / CATALOG).write_text("не json", encoding="utf-8")
    assert load_catalog(cfg) == []


def test_the_catalog_is_reread_when_the_file_changes(tmp_path):
    cfg = library(tmp_path, [row("calm-01")])
    assert len(load_catalog(cfg)) == 1
    path = Path(cfg.music_dir) / CATALOG
    rows = [row("calm-01"), row("calm-02")]
    path.write_text(json.dumps(rows), encoding="utf-8")
    (Path(cfg.music_dir) / "calm-02.m4a").write_bytes(b"\0")
    import os

    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert len(load_catalog(cfg)) == 2


def test_the_model_sees_keys_and_moods_but_no_files_or_links():
    """Модель называет трек ключом. Имя файла и ссылка ей не нужны и в ответ
    попасть не должны: путь в ответе модели — это путь, который кто-то откроет."""
    table = tracks_table([Track(**row("calm-01")), Track(**row("upbeat-01", mood="бодрый"))])
    assert table.splitlines()[0].startswith("calm-01 | спокойный | ")
    assert "upbeat-01 | бодрый" in table
    assert ".m4a" not in table and "http" not in table


def test_the_shipped_library_is_usable():
    """Каждая строка каталога в репозитории — трек, который бот реально поставит:
    лицензия без подписи автора, файл на месте, страница трека для перепроверки."""
    path = REPO_ROOT / "assets" / "music" / CATALOG
    if not path.exists():
        pytest.skip("библиотека не собрана")
    rows = json.loads(path.read_text(encoding="utf-8"))
    got = load_catalog(Settings(_env_file=None))
    assert len(got) == len(rows), "часть строк каталога не загрузилась — см. предупреждения"
    for track in got:
        assert track.license == "cc0" and track.source.startswith("https://"), track.key
        assert track.mood and 20 <= track.duration <= 120, track.key
    assert any(t.energy == "low" for t in got), "без модели трек берётся из спокойных"


# ------------------------------------------------------------------ выбор


def test_without_a_model_the_track_is_a_calm_one_and_always_the_same():
    tracks = [
        Track(**row("calm-01")), Track(**row("calm-02")),
        Track(**row("energetic-01", energy="high")),
    ]
    first = fallback_track(TEXT, tracks)
    assert first is not None and first.energy == "low"
    assert fallback_track(TEXT, tracks) is first
    assert {fallback_track(f"ролик {i}", tracks).key for i in range(40)} == {"calm-01", "calm-02"}
    # Спокойных нет — берётся любой: галочка включена, тишина хуже.
    loud = [Track(**row("energetic-01", energy="high"))]
    assert fallback_track(TEXT, loud).key == "energetic-01"
    assert fallback_track(TEXT, []) is None


async def test_the_model_picks_from_the_table(tmp_path):
    cfg = library(tmp_path, [row("calm-01"), row("upbeat-01", mood="бодрый", energy="mid")])
    llm = FakeLLM({"mood": "деловой, бодрый", "tracks": ["upbeat-01"]})
    report: dict = {}
    track = await pick_music(TEXT, 30.0, llm=llm, language="ru", cfg=cfg, report=report)
    assert track is not None and track.key == "upbeat-01"
    assert report == {"from_llm": True, "mood": "деловой, бодрый"}
    name, variables, tier = llm.calls[0]
    assert (name, tier) == ("pick_music", "cheap")
    assert "upbeat-01 | бодрый" in variables["tracks_table"] and TEXT in variables["transcript"]


async def test_one_of_several_named_tracks_is_taken_and_the_choice_is_stable(tmp_path):
    """Несколько подходящих — программа берёт один по тексту ролика: тот же ролик
    всегда с тем же треком, а разные ролики автора не звучат одинаково."""
    cfg = library(tmp_path, [row(f"calm-0{i}") for i in range(1, 5)])
    named = ["calm-02", "calm-04", "calm-02", "нет-такого"]
    keys = set()
    for i in range(30):
        text = f"ролик номер {i}"
        a = await pick_music(text, 30.0, llm=FakeLLM({"mood": "", "tracks": named}), cfg=cfg)
        b = await pick_music(text, 30.0, llm=FakeLLM({"mood": "", "tracks": named}), cfg=cfg)
        assert a is not None and b is not None and a.key == b.key
        keys.add(a.key)
    assert keys == {"calm-02", "calm-04"}


async def test_an_empty_answer_means_the_reel_needs_no_music(tmp_path):
    cfg = library(tmp_path, [row("calm-01")])
    llm = FakeLLM({"mood": "траур", "tracks": []})
    assert await pick_music(TEXT, 30.0, llm=llm, cfg=cfg) is None


@pytest.mark.parametrize("llm", [
    FakeLLM({"mood": "бодрый", "tracks": ["придумал-сам", "https://example.org/a.mp3"]}),
    FakeLLM(error=LLMUnavailable("нет ключа")),
    FakeLLM(error=LLMBadOutput("не JSON")),
    None,
])
async def test_a_failed_answer_still_gives_a_calm_track(tmp_path, llm):
    """Галочка включена, а модель подвела: ролик не остаётся без музыки, но и
    выдуманное имя никуда не уходит — берётся спокойный трек библиотеки."""
    cfg = library(tmp_path, [row("calm-01"), row("energetic-01", energy="high")])
    report: dict = {}
    track = await pick_music(TEXT, 30.0, llm=llm, cfg=cfg, report=report)
    assert track is not None and track.key == "calm-01"
    assert report["from_llm"] is False


async def test_an_empty_library_never_calls_the_model(tmp_path):
    cfg = Settings(_env_file=None, music_dir=tmp_path / "пусто")
    llm = FakeLLM({"mood": "", "tracks": ["calm-01"]})
    assert await pick_music(TEXT, 30.0, llm=llm, cfg=cfg) is None
    assert llm.calls == [], "платный вызов ради пустой библиотеки"
    assert await pick_music("  ", 30.0, llm=llm, cfg=library(tmp_path, [row("calm-01")])) is None


async def test_a_long_transcript_is_cut_for_the_model(tmp_path):
    cfg = library(tmp_path, [row("calm-01")])
    llm = FakeLLM({"mood": "", "tracks": ["calm-01"]})
    await pick_music("слово " * 2000, 170.0, llm=llm, cfg=cfg)
    assert len(llm.calls[0][1]["transcript"]) <= 1500


# ------------------------------------------------------------------ запись в лист


def test_the_track_goes_into_the_edl_by_file_name_at_the_measured_level():
    """Громкость считается от цели голоса: трек на 14 LU под голосом и приседает
    на 6 дБ — те же числа, что у «Музыки из рилса», мерены на живой речи."""
    track = Track(**row("calm-01"))
    spec = music_spec(track, target_lufs=-16.0)
    assert (spec.source, spec.track) == ("asset", "calm-01.m4a")
    assert spec.gain_db == -14.0 and spec.duck_db == REEL_DUCK_DB
    assert music_spec(track, target_lufs=-14.0).gain_db == -12.0
