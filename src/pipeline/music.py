"""Фоновая музыка по смыслу ролика: библиотека треков и выбор из неё.

Человек включает галочку «Фоновая музыка» — и ролик получает тихий трек под
голосом, подобранный под то, о чём и каким тоном в нём говорят.

Как это устроено:

1. Библиотека — файлы в `assets/music/` и их описание `catalog.json`: ключ,
   настроение, пара фраз о треке, лицензия и откуда он взят. Собирает её
   `tools/music_fetch.py`.
2. Модель (тир `cheap`, промпт `pick_music`) читает расшифровку и таблицу
   треков и называет до трёх подходящих ключей — или ни одного, если музыка
   ролику мешает. Найти трек «в интернете» она не может: вне таблицы треков для
   неё не существует, а выдуманный ключ отбрасывается здесь.
3. Из названных программа берёт один — по хешу расшифровки, чтобы один и тот же
   ролик всегда получал один и тот же трек, а разные ролики одного автора не
   звучали одинаково.
4. Выбор уезжает в лист (`EDL.music`), дальше работает обычное сведение
   (`audio_post.build_audio`): зацикливание, фейды, приседание под голос.
   Перерендер получает лист и кладёт тот же трек.

Нет модели — трек берётся из спокойных (`energy == "low"`): фон, который не
спорит ни с какой речью. Нет библиотеки — музыки нет, ролик собирается как раньше.

Про лицензии. В библиотеку попадают только треки, которые можно вставлять в
чужой коммерческий ролик БЕЗ указания автора (`FREE_LICENSES`). CC BY сюда не
годится: бот не может заставить человека подписать автора под рилсом, а без
подписи это нарушение лицензии — от имени нашего пользователя. Запись каталога с
другой лицензией не загружается вовсе, и это проверка кода, а не договорённость.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.config import Settings, settings as default_settings
from src.core.edl import Music
from src.llm.client import LLMBadOutput, LLMClient, LLMUnavailable

log = logging.getLogger(__name__)

CATALOG = "catalog.json"
# Лицензии, с которыми трек можно класть под чужой ролик без подписи автора:
# CC0, общественное достояние и собственная запись владельца продукта.
FREE_LICENSES = frozenset({"cc0", "pdm", "own"})
# К этой громкости приведён каждый файл библиотеки (`tools/music_fetch.py`).
# От неё считается, насколько трек убавить под голос (`music_spec`).
LIBRARY_LUFS = -16.0
# Сколько текста ролика показывать модели: настроение слышно с первых фраз, а
# трёхминутная расшифровка целиком только удорожает вызов.
TRANSCRIPT_CHARS = 1500

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


class Track(BaseModel):
    """Трек библиотеки — строка `catalog.json`."""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(pattern=_KEY.pattern)
    file: str
    title: str = ""
    # Настроение парой слов и описание — то, что читает модель. По-русски:
    # расшифровки русские, и «тёплый, спокойный» ей ближе набора тегов.
    mood: str
    about: str = ""
    energy: Literal["low", "mid", "high"] = "mid"
    duration: float = 0.0
    license: str
    author: str = ""
    source: str = ""  # страница трека — чтобы лицензию можно было перепроверить


def _catalog_path(cfg: Settings) -> Path:
    return Path(cfg.music_dir) / CATALOG


@lru_cache(maxsize=4)
def _read_catalog(path: str, mtime: float) -> tuple[Track, ...]:
    """Каталог с диска. `mtime` — только ключ кеша: файл обновили — читаем заново."""
    root = Path(path).parent
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("каталог музыки %s не читается: %s", path, e)
        return ()
    if isinstance(rows, dict):
        rows = rows.get("tracks") or []
    tracks: list[Track] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        try:
            track = Track.model_validate(row)
        except ValidationError as e:
            log.warning("каталог музыки: строка пропущена (%s): %r", e.errors()[0]["msg"], row)
            continue
        if track.license.lower() not in FREE_LICENSES:
            log.warning(
                "каталог музыки: %s пропущен — лицензия %r требует подписи автора или "
                "не проверена", track.key, track.license,
            )
            continue
        if track.key in seen:
            log.warning("каталог музыки: ключ %s повторяется — взят первый", track.key)
            continue
        if not (root / track.file).is_file():
            log.warning("каталог музыки: у %s нет файла %s", track.key, track.file)
            continue
        seen.add(track.key)
        tracks.append(track)
    return tuple(tracks)


def load_catalog(cfg: Settings | None = None) -> list[Track]:
    """Треки, которые можно ставить прямо сейчас: описаны, разрешены, лежат на диске."""
    cfg = cfg or default_settings
    path = _catalog_path(cfg)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    return list(_read_catalog(str(path), mtime))


def tracks_table(tracks: Sequence[Track]) -> str:
    """Таблица для модели: ключ, настроение, описание. Файлов и ссылок в ней нет."""
    return "\n".join(
        f"{t.key} | {t.mood} | {t.about or t.title}".rstrip(" |") for t in tracks
    )


def _stable(text: str, n: int) -> int:
    """Номер из `n` по тексту — один и тот же на любой машине и в любом запуске."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big") % n


def fallback_track(text: str, tracks: Sequence[Track]) -> Track | None:
    """Трек без модели: один из спокойных, а нет спокойных — любой.

    Спокойный фон не спорит ни с какой речью; энергичный под рассказ о болезни
    звучал бы издёвкой, а проверить смысл здесь некому.
    """
    if not tracks:
        return None
    calm = [t for t in tracks if t.energy == "low"] or list(tracks)
    return calm[_stable(text, len(calm))]


async def pick_music(
    text: str,
    duration: float,
    *,
    llm: LLMClient | None,
    language: str = "",
    cfg: Settings | None = None,
    report: dict[str, Any] | None = None,
) -> Track | None:
    """Трек под ролик или `None` — библиотека пуста либо музыка ролику мешает.

    Никогда не бросает: музыка необязательна, ролик важнее. `report` — куда
    записать, кто выбирал (`from_llm`) и какое настроение услышала модель.
    """
    cfg = cfg or default_settings
    if report is not None:
        report.update(from_llm=False, mood="")
    try:
        tracks = load_catalog(cfg)
        if not tracks or not text.strip():
            return None
        if llm is None or not llm.ready:
            return fallback_track(text, tracks)

        from src.llm.schemas import MUSIC_SCHEMA

        try:
            result = await llm.complete(
                "pick_music",
                {
                    "language": language or cfg.whisper_language or "auto",
                    "duration": f"{duration:.0f}",
                    "transcript": text.strip()[:TRANSCRIPT_CHARS],
                    "tracks_table": tracks_table(tracks),
                },
                MUSIC_SCHEMA,
                "cheap",
                max_tokens=600,
            )
        except (LLMUnavailable, LLMBadOutput) as e:
            log.warning("музыка: модель не ответила (%s) — беру спокойный трек", e)
            return fallback_track(text, tracks)

        by_key = {t.key: t for t in tracks}
        named = [str(k).strip().lower() for k in result.data.get("tracks") or []]
        mood = str(result.data.get("mood") or "").strip()
        if report is not None:
            report.update(from_llm=True, mood=mood)
        if not named:
            log.info("музыка: модель решила, что ролику она не нужна (%s)", mood or "—")
            return None
        picked = list(dict.fromkeys(k for k in named if k in by_key))
        if not picked:
            # Назвала то, чего в таблице нет. Это не «музыка не нужна», а сбой
            # ответа — ролик с включённой галочкой без музыки не оставляем.
            log.warning("музыка: модель назвала неизвестные треки %s — беру спокойный", named)
            if report is not None:
                report["from_llm"] = False
            return fallback_track(text, tracks)
        track = by_key[picked[_stable(text, len(picked))]]
        log.info("музыка: %s (%s) из %s, настроение «%s»", track.key, track.mood, picked, mood)
        return track
    except Exception:  # noqa: BLE001 — музыка не имеет права уронить ролик
        log.exception("музыка: подбор упал — ролик выйдет без неё")
        return None


def music_spec(track: Track, *, target_lufs: float) -> Music:
    """Трек библиотеки → запись листа: насколько убавить и насколько приседать.

    Числа те же, что у «Музыки из рилса» (`audio_post.REEL_*`), и по той же
    причине: они мерены на живой речи — при меньшей громкости трек под голосом
    пропадал совсем. Файл библиотеки приведён к `LIBRARY_LUFS`, поэтому убавка
    считается, а не подбирается на слух для каждого трека.
    """
    from src.pipeline.audio_post import REEL_DUCK_DB, REEL_MUSIC_BELOW_VOICE_DB

    gain = (target_lufs - REEL_MUSIC_BELOW_VOICE_DB) - LIBRARY_LUFS
    return Music(source="asset", track=track.file, gain_db=round(gain, 1), duck_db=REEL_DUCK_DB)
