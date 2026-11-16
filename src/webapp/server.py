"""HTTP-сторона Mini App: статика и JSON API. Живёт в процессе бота.

Почему в процессе бота, а не отдельным сервисом: приложению нужен тот же `Bot` —
выставить счёт на звёзды, написать пользователю статус, отдать ролик в чат, — и
те же наблюдатели прогресса, что у загрузки из чата. Второй процесс держал бы
второе соединение с Telegram ради того, что здесь уже есть.

Кто пришёл, решает только подпись `initData` (заголовок `Authorization: tma …`).
Ни одному полю из тела запроса про пользователя сервер не верит.

Загрузка идёт кусками. Бесплатный туннель Cloudflare режет тело запроса на
100 МБ, а трёхминутное видео с телефона бывает больше; заодно оборванную на
мобильной сети загрузку можно продолжить с того байта, на котором она встала.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import hmac
import json
import os
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import LabeledPrice
from aiohttp import web

from src.core.config import CLAIMED_PREFIX, MUSIC_PREFIX, UPLOAD_PREFIX, Settings
from src.core.geometry import load_metrics
from src.core.i18n import LANGS, error_text, lang_of, stage_title, t, tokens_text
from src.core.options import ACCENTS, FONTS, JobOptions, theme_label
from src.core.preset import available, load_preset
from src.core.referral import code_of
from src.db import repo
from src.db.models import Job, JobStatus
from src.payments import payhot as payhot_mod
from src.storage.s3 import SOURCES, storage
from src.webapp.auth import AuthError, WebAppUser, validate_init_data

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")

BOT_KEY = web.AppKey("bot", Bot)
CFG_KEY = web.AppKey("cfg", Settings)
USER_KEY = web.RequestKey("webapp_user", WebAppUser)
LANG_KEY = web.RequestKey("webapp_lang", str)


# ------------------------------------------------------------------ вспомогательное


def _json_error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


async def _lang(request: web.Request) -> str:
    """Язык интерфейса пользователя. Читается из БД один раз на запрос и только когда нужен."""
    if LANG_KEY in request:
        return request[LANG_KEY]
    u: WebAppUser = request[USER_KEY]
    try:
        db_user = await repo.get_user(u.id)
    except Exception:  # noqa: BLE001 — нет базы: язык по клиенту Telegram
        db_user = None
    lang = lang_of(db_user) if db_user else lang_of(u)
    request[LANG_KEY] = lang
    return lang


async def _fail(request: web.Request, status: int, key: str, *, extra: dict[str, Any] | None = None,
                **params: Any) -> web.Response:
    return _json_error(status, t(key, await _lang(request), **params), **(extra or {}))


def media_token(job_id: str, secret: str, *, ttl_s: int = 6 * 3600, now: float | None = None) -> str:
    """Подписанная ссылка на ролик для тега <video>.

    Браузер грузит видео сам и заголовок авторизации к нему не приложит, поэтому
    право посмотреть ролик едет в адресе: id задачи, срок и подпись ключом бота.
    """
    exp = int((time.time() if now is None else now) + ttl_s)
    return f"{exp}.{_media_sig(job_id, exp, secret)}"


def _media_sig(job_id: str, exp: int, secret: str) -> str:
    # Ключ выводится из токена, а не равен ему: подпись ссылки — не то место,
    # где сырой токен бота должен работать ключом.
    key = hashlib.sha256(b"reelbot-media:" + secret.encode()).digest()
    return hmac.new(key, f"{job_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def check_media_token(job_id: str, token: str, secret: str, *, now: float | None = None) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_media_sig(job_id, exp, secret), sig)


def _upload_dir(cfg: Settings, upload_id: str) -> Path:
    # Папка лежит прямо в work_dir: её подберёт почасовая уборка старых папок,
    # если пользователь закрыл приложение посреди загрузки.
    return Path(cfg.work_dir) / f"{UPLOAD_PREFIX}{upload_id}"


# Сколько незавершённых приёмов разрешено одному пользователю. Приём занимает
# диск до самого конца загрузки, а брошенную папку убирает только уборщик.
# При разрешённом гигабайте на файл десяток брошенных приёмов съедает весь
# свободный диск сервера — вместе с местом под Postgres, который там же.
# Два — чтобы повтор на оборванной мобильной сети не упирался в отказ.
# Четыре, а не два: ролик «Два видео» — это два приёма разом, и один
# брошенный приём (закрыли приложение на середине) иначе запирал бы режим до
# истечения заявки.
MAX_OPEN_UPLOADS = 4
# Запас свободного места, который приёму не отдаётся ни при каких условиях:
# под базу, журналы и рабочие папки самого монтажа.
DISK_RESERVE_MB = 4096
# Сколько живёт заявка на место без единого нового куска. Заявка держит место
# за собой (иначе учёт обещанного ничего не стоит), а объявить гигабайт и
# ничего не прислать стоит одного запроса: пяти аккаунтов хватило бы, чтобы
# закрыть загрузку всем до прихода уборщика. Срок продлевает каждый кусок,
# поэтому живой загрузке он не мешает: кусок — 8 МБ, и четверть часа на него
# не уходит даже на плохой мобильной сети.
UPLOAD_LEASE = 15 * 60


def _claim_upload(folder: Path) -> Path | None:
    """Забирает приём себе под сборку. None — его уже забрал кто-то другой.

    Переименование атомарно, и это здесь главное. Два «Готово» разом (двойной
    тап, повтор по тайм-ауту) иначе создали бы две задачи за один файл — то
    есть списали бы два ролика. Выигрывает ровно один вызов `rename`.

    Заодно папка уходит из-под уборки приёмов: дальше идут ffprobe и заливка в
    хранилище, новых кусков не будет, и срок заявки на большом файле мог бы
    истечь прямо посреди сборки.
    """
    claimed = folder.with_name(f"{CLAIMED_PREFIX}{folder.name[len(UPLOAD_PREFIX):]}")
    try:
        folder.rename(claimed)
    except OSError:
        return None
    return claimed


def _sweep_stale_uploads(cfg: Settings) -> list[tuple[Path, dict[str, Any]]]:
    """Сносит заявки с истёкшим сроком и отдаёт живые, старые первыми.

    Снос здесь, а не только в уборщике: просроченная заявка иначе держала бы и
    место, и квоту своего хозяина — то есть пользователь, у которого
    приложение закрылось посреди загрузки, не смог бы начать заново.
    """
    root = Path(cfg.work_dir)
    alive: list[tuple[Path, dict[str, Any]]] = []
    stale_before = time.time() - UPLOAD_LEASE
    for folder in root.glob(f"{UPLOAD_PREFIX}*") if root.exists() else []:
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            data = folder / "data"
            # Срок считается по файлу данных: его трогает каждый кусок.
            touched = (data if data.exists() else folder).stat().st_mtime
        except (OSError, ValueError):
            continue  # папка недописана или уже убрана — не наше дело
        if not isinstance(meta, dict):
            continue
        if touched < stale_before:
            shutil.rmtree(folder, ignore_errors=True)
            continue
        alive.append((folder, meta))
    return sorted(alive, key=lambda pair: pair[0].stat().st_mtime)


def _renew_finished_uploads(cfg: Settings, user_id: int, current: Path) -> None:
    """Продлевает срок догруженных приёмов пользователя, пока идёт его другой.

    Ролик «Два видео» — два приёма подряд: нижнее догружено, а верхнее ещё
    идёт, и «Готово» придёт только после него. Срок заявки считается по
    последнему куску, так что большое верхнее на мобильной сети пережило бы
    нижнее, и пользователь получил бы 404, залив оба файла. Продлеваются только
    ДОГРУЖЕННЫЕ и только пока у хозяина есть живая загрузка: брошенный файл
    по-прежнему уходит по сроку.
    """
    root = Path(cfg.work_dir)
    now = time.time()
    for folder in root.glob(f"{UPLOAD_PREFIX}*") if root.exists() else []:
        if folder == current:
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            data = folder / "data"
            if meta.get("user_id") == user_id and data.stat().st_size == meta.get("size"):
                os.utime(data, (now, now))
        except (OSError, ValueError, AttributeError):
            continue  # папку убрали или забрали под сборку — не наше дело


# ------------------------------------------------------------ музыка из рилса

# Сколько ссылок в минуту проверяет один человек. Каждая проверка — поход
# сервера в Instagram: без предела один аккаунт гонял бы его с нашего IP, пока
# Instagram не закроет нам выгрузку для всех. Счёт в памяти: приложение — один
# процесс (служба reelbot-bot).
MUSIC_CHECKS_PER_MINUTE = 6
# Сколько скачиваний идёт разом на весь сервер. Каждое — процесс yt-dlp.
MAX_PARALLEL_FETCHES = 2
MUSIC_CHECKS_KEY = web.AppKey("music_checks", dict)
_fetch_gate = asyncio.Semaphore(MAX_PARALLEL_FETCHES)
_MUSIC_STATUS = {"bad_link": 400, "no_audio": 422, "unavailable": 422,
                 "instagram_down": 502, "too_big": 413, "unsupported": 422, "not_media": 422}


def _music_dir(cfg: Settings, music_id: str) -> Path:
    return Path(cfg.work_dir) / f"{MUSIC_PREFIX}{music_id}"


def _allow_music_check(checks: dict[int, list[float]], user_id: int) -> bool:
    now = time.time()
    recent = [t0 for t0 in checks.get(user_id, []) if now - t0 < 60]
    allowed = len(recent) < MUSIC_CHECKS_PER_MINUTE
    if allowed:
        recent.append(now)
    checks[user_id] = recent
    return allowed


def _own_music(cfg: Settings, user_id: int, music_id: Any) -> Path | None:
    """Файл трека этого пользователя или None — чужой, неизвестный, убранный."""
    if not isinstance(music_id, str) or not _UPLOAD_ID.match(music_id):
        return None
    folder = _music_dir(cfg, music_id)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("user_id") != user_id:
        return None
    return next(iter(sorted(folder.glob("track.*"))), None)


def _drop_older_tracks(cfg: Settings, user_id: int, *, keep: Path, started: float) -> None:
    """У человека живёт один трек — последний проверенный.

    Шесть проверок в минуту по 25 МБ, пока уборщик не пришёл через два часа, —
    это гигабайты треков одного человека. Прежние ему уже не нужны: в `finish`
    уходит только последний `music_id`.

    «Последний» — по моменту, когда человек вставил ссылку, а не когда звук
    скачался: медленная проверка прежней ссылки, закончив позже, не имеет права
    снести свежий трек, который приложение уже показало «готов».
    """
    root = Path(cfg.work_dir)
    for folder in root.glob(f"{MUSIC_PREFIX}*"):
        if folder == keep:
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # недописанная папка соседней проверки — не трогаем
        if not isinstance(meta, dict) or meta.get("user_id") != user_id:
            continue
        if float(meta.get("started") or 0) < started:
            shutil.rmtree(folder, ignore_errors=True)


async def music_check(request: web.Request) -> web.Response:
    """Ссылка на рилс → звук у нас в папке → `music_id` для `finish`.

    Проверка сразу, а не в монтаже: человек видит «нет звука» или «закрыт»,
    пока ещё на экране оформления, и может вставить другую ссылку.
    """
    from src.pipeline import reel_audio

    cfg = request.app[CFG_KEY]
    user: WebAppUser = request[USER_KEY]
    try:
        body = await request.json()
    except ValueError:
        body = {}
    db_user = await _db_user(request)
    if db_user.is_blocked:
        return await _fail(request, 403, "api.blocked")
    link = reel_audio.parse_link(str((body or {}).get("url") or "")[:2000])
    if link is None:
        # До лимита: недописанная ссылка, пока её набирают, — не поход в
        # сеть, и съедать ею проверки настоящей ссылки нельзя.
        return await _fail(request, 400, "music.bad_link")
    if link.kind == "youtube":
        # YouTube не пускает серверы без входа в Google-аккаунт (проверено
        # 16.11.2026) — честно говорим сразу, а не после минуты ожидания.
        return await _fail(request, 422, "music.unsupported")
    if not _allow_music_check(request.app[MUSIC_CHECKS_KEY], user.id):
        return await _fail(request, 429, "music.too_many")
    # Тот же запас диска, что у приёма видео: под базу и монтаж место остаётся
    # при любом потоке ссылок.
    free = shutil.disk_usage(cfg.work_dir).free - DISK_RESERVE_MB * 1024 * 1024
    if free < reel_audio.REEL_MAX_BYTES:
        return await _fail(request, 507, "api.no_space")
    music_id = uuid.uuid4().hex
    folder = _music_dir(cfg, music_id)
    started = time.time()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        async with _fetch_gate:
            if link.kind == "file":
                track = await reel_audio.fetch_file_audio(link.url, folder)
            else:
                url = link.url
                if link.kind == "instagram_share":
                    url = (await reel_audio.resolve_share(reel_audio.ReelLink("share", url))).url
                platform = "tiktok" if link.kind == "tiktok" else "instagram"
                track = await reel_audio.fetch_reel_audio(
                    url, folder, extractors=reel_audio.EXTRACTORS[platform])
        (folder / "meta.json").write_text(
            json.dumps({"user_id": user.id, "duration": track.duration, "started": started}),
            encoding="utf-8",
        )
        _drop_older_tracks(cfg, user.id, keep=folder, started=started)
    except reel_audio.ReelAudioError as e:
        shutil.rmtree(folder, ignore_errors=True)
        return await _fail(request, _MUSIC_STATUS.get(e.code, 502), f"music.{e.code}")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return web.json_response({"music_id": music_id, "duration": track.duration})


def _promised_bytes(open_uploads: list[tuple[Path, dict[str, Any]]]) -> int:
    """Сколько байт уже обещано приёмам, но ещё не легло на диск.

    Без этого счёта проверка места бессмысленна: байты незавершённого приёма на
    диске не лежат, и `disk_usage` показывает их место свободным. Лимит на
    пользователя тут не помогает — десять человек по два приёма обещают
    двадцать гигабайт на диске, где их нет.
    """
    promised = 0
    for folder, meta in open_uploads:
        try:
            have = (folder / "data").stat().st_size
        except OSError:
            have = 0
        promised += max(0, int(meta.get("size") or 0) - have)
    return promised


def _job_view(job: Job, *, live: dict[str, Any] | None, secret: str, lang: str, cfg: Settings) -> dict[str, Any]:
    from src.pipeline.job import STAGE_KEYS_BY_TITLE

    progress = job.progress
    status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    stage_key = STAGE_KEYS_BY_TITLE.get(job.stage or "")
    if live and status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        stage_key = live.get("stage_key") or STAGE_KEYS_BY_TITLE.get(live.get("stage") or "") or stage_key
        progress = float(live.get("progress") or progress or 0.0)
    stage = stage_title(stage_key, lang)
    if stage is None and status == JobStatus.QUEUED.value:
        stage = t("stage.queued", lang)
    error = None
    if status == JobStatus.FAILED.value:
        # Пользователю — фраза по коду на его языке, а не русский текст из `jobs.error`.
        error = error_text(job.error_code, lang, min_s=cfg.min_video_seconds, max_m=cfg.max_video_minutes)
    view: dict[str, Any] = {
        "id": str(job.id),
        "status": status,
        "stage": stage,
        "progress": round(float(progress or 0.0), 3),
        "preset": job.preset,
        "options": JobOptions.from_stored(job.options).model_dump(),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "source_duration": job.source_duration,
        "result_duration": job.result_duration,
        "error": error,
        "delivered": job.delivered,
        "video_url": None,
        "purged": job.files_purged_at is not None,
    }
    if status == JobStatus.DONE.value and job.result_key:
        view["video_url"] = f"/media/{job.id}.mp4?t={media_token(str(job.id), secret)}"
    return view


# ------------------------------------------------------------------ авторизация


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    cfg = request.app[CFG_KEY]
    header = request.headers.get("Authorization", "")
    init_data = header[4:] if header.startswith("tma ") else ""
    try:
        user = validate_init_data(init_data, cfg.bot_token, max_age_s=cfg.webapp_auth_max_age_s)
    except AuthError as e:
        return _json_error(401, str(e))
    request[USER_KEY] = user
    return await handler(request)


async def _db_user(request: web.Request):
    u: WebAppUser = request[USER_KEY]
    cfg = request.app[CFG_KEY]
    return await repo.upsert_user(
        u.id,
        username=u.username,
        first_name=u.first_name,
        language_code=u.language_code,
        is_admin=u.id in cfg.admin_ids,
    )


# ------------------------------------------------------------------ API


def cap_em(file: str | None) -> float | None:
    """Высота прописных в долях em — из тех же метрик, что считают кадр.

    Нужна приложению, чтобы показать выбранный шрифт тем кеглем, которым его
    покажет ролик: `options.apply_font` нормирует кегль именно по ней. Второй
    источник правды тут разошёлся бы молча, поэтому число не переписывается в
    каталог руками, а читается из выгрузки.
    """
    if not file:
        return None
    try:
        m = load_metrics(file)
    except Exception:  # noqa: BLE001 — без метрик приложение просто не масштабирует
        return None
    return round(m.cap_height / m.units_per_em, 4) if m.units_per_em else None


async def bootstrap(request: web.Request) -> web.Response:
    """Всё, что приложению нужно для первого экрана, одним запросом."""
    cfg = request.app[CFG_KEY]
    bot = request.app[BOT_KEY]
    user = await _db_user(request)
    lang = lang_of(user)
    request[LANG_KEY] = lang
    me = await bot.me()

    loaded = []
    for name in available():
        try:
            loaded.append((name, load_preset(name)))
        except Exception:  # noqa: BLE001 — битый пресет не должен ронять приложение
            continue
    # Тема по умолчанию — первой: это первая плитка, которую видит пользователь.
    # Дальше порядок задаёт сама тема (`order`), а не алфавит: тем семнадцать, и
    # алфавит перемешал бы наборные с выверенными по эталону.
    loaded.sort(key=lambda item: (item[0] != cfg.default_preset, item[1].order, item[0]))
    presets = [
        {
            "name": name,
            "title": p.title or name,
            # Понятная подпись под названием: «Chisel» не говорит ничего тому,
            # кто видит тему впервые, а «Классика» — говорит.
            "label": theme_label(name, lang, p.title or name),
            "font": p.font.family,
            "font_file": p.font.file,
            "letter_spacing": p.font.letter_spacing,
            "uppercase": p.font.uppercase,
            # Второй шрифт темы: им набрано выделенное слово. Превью обязано
            # показать пару, иначе тема выглядит как её соседка без пары.
            "accent_font": (
                {
                    "family": p.accent_font.family,
                    "file": p.accent_font.file,
                    "weight": p.accent_font.weight,
                    "scale": p.accent_font.scale,
                }
                if p.accent_font
                else None
            ),
            # Числа раскладки: превью рисует ТЕМУ, а не одну композицию на всех
            # — кегль, межстрочие, место в кадре, плашку и во сколько раз
            # крупнее выделенное слово. Пиксели тут кадровые (высота 1920), и
            # приложение пересчитывает их в свои само.
            "size": p.font.size,
            "cap_em": cap_em(p.font.file),
            "line_height": p.caption.line_height,
            "position": p.caption.position,
            "margin": p.caption.margin,
            "accent_scale": p.animation.accent_scale,
            "colors": {"text": p.colors.active, "accent": p.colors.accent},
            "panel": {
                "enabled": p.panel.enabled,
                "color": p.panel.color,
                "opacity": p.panel.opacity,
                "radius": p.panel.radius,
                "padding_x": p.panel.padding_x,
                "padding_y": p.panel.padding_y,
            },
            # Числа движения — чтобы превью показывало ту тему, которую
            # выбрали, а не одно и то же для всех. Подъём приводится к
            # ДОЛЕ кегля: превью рисуется своим шрифтом в своём масштабе,
            # и пиксели кадра 1920 там ничего не значат.
            "motion": {
                "word_in_ms": p.animation.word_in_ms,
                "word_rise_em": round(p.animation.word_rise_px / max(1, p.font.size), 3),
            },
        }
        for name, p in loaded
    ]
    return web.json_response(
        {
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "is_admin": user.is_admin,
                "balance": user.balance,
                "videos_done": user.videos_done,
                "preset": user.preset,
                # Запомненные цвет и шрифт. Приложение берёт их вместо умолчаний.
                "settings": user.settings or {},
                "lang": lang,
            },
            "bot_username": me.username,
            # Название — одно на всё (`Settings.brand_name`), а не то, что сейчас
            # записано именем бота в Telegram: эти два места и расходились.
            "bot_name": cfg.brand_name or me.first_name,
            "presets": presets,
            "accents": [
                {"key": k, "title": titles.get(lang, titles["ru"]), "color": color}
                for k, (titles, color) in ACCENTS.items()
            ],
            # Шрифты той же формы, что и цвета: ключ, подпись и то, чем рисовать
            # превью. `file` у «как в теме» пустой — шрифт берётся у выбранной
            # темы. `cap_em` — высота прописных: по ней приложение пересчитывает
            # кегль превью ровно так же, как это делает `options.apply_font` в
            # пайплайне, иначе превью обещало бы не тот размер.
            "fonts": [
                {
                    "key": k,
                    "title": titles.get(lang, titles["ru"]),
                    "family": choice.family if choice else None,
                    "file": choice.file if choice else None,
                    "cap_em": cap_em(choice.file) if choice else None,
                }
                for k, (titles, choice) in FONTS.items()
            ],
            "packages": _packages_view(cfg),
            # Способы оплаты по порядку показа: включённые способы PayHot и звёзды.
            "pay_methods": _pay_methods_view(cfg),
            # Цена ролика в токенах: приложение по ней решает, хватает ли баланса.
            "video_cost": cfg.tokens_per_video,
            # Монтаж сейчас бесплатный для всех: кнопка не ведёт в тарифы и не
            # обещает списания.
            "free": cfg.free_videos,
            # Сколько роликов даётся новичку на пробу: первый экран говорит об
            # этом сразу, вместе с ценой следующего.
            "trial": max(0, cfg.trial_videos),
            "trial_tokens": cfg.trial_tokens,
            "limits": {
                "min_seconds": cfg.min_video_seconds,
                "max_minutes": cfg.max_video_minutes,
                "max_upload_mb": cfg.webapp_max_upload_mb,
                "chunk_bytes": cfg.webapp_chunk_mb * 1024 * 1024,
            },
            "defaults": JobOptions().model_dump(),
            "retention_days": cfg.storage_retention_days,
            # Приглашения. Ссылку собирает приложение из имени бота и кода: так
            # она одна и та же в чате, в «Поделиться» и в поле для копирования.
            "referral": (
                {"code": code_of(user.id), "percent": cfg.referral_percent, "credit": user.ref_credit}
                if cfg.referral_percent > 0
                else None
            ),
        }
    )


def _packages_view(cfg: Settings) -> list[dict[str, Any]]:
    """Пакеты для «Тарифов»: токены, «до N роликов», цена и выгода.

    Выгода — от цены токена в самом маленьком пакете, в рублях (если они
    заданы у всех пакетов), иначе в звёздах. Отрицательная или нулевая не
    показывается: пакет, где токен дороже, «выгодой» не назовёшь.
    """
    packs = cfg.token_packages
    in_rub = bool(packs) and all(p.rub for p in packs)

    def per_token(p) -> float:
        return (p.rub if in_rub else p.stars) / p.tokens

    base = per_token(packs[0]) if packs else 0.0
    return [
        {
            "tokens": p.tokens,
            "stars": p.stars,
            "rub": p.rub,
            "videos": p.tokens // cfg.tokens_per_video,
            "popular": p.popular,
            "save": max(0, round((1 - per_token(p) / base) * 100)) if base else 0,
        }
        for p in packs
    ]


def _pay_methods_view(cfg: Settings) -> list[dict[str, Any]]:
    """Способы в «Тарифах». Цена у PayHot — рубли пакета (карту PayHot выставит
    в евро по своему курсу, итог покажет на странице оплаты), у звёзд — звёзды."""
    out: list[dict[str, Any]] = []
    wallets: list[dict[str, Any]] = []
    for m in payhot_mod.methods(cfg):
        min_rub = m.min_amount_minor // 100 if m.currency == "RUB" and m.min_amount_minor else 0
        # Карта и кошельки выставляются в евро по курсу PayHot: рубли — ориентир («≈»).
        view = {"key": m.key, "kind": "payhot", "min_rub": min_rub, "approx": m.currency != "RUB"}
        (wallets if m.key in WALLETS else out).append(view)
    out.append({"key": "stars", "kind": "stars", "min_rub": 0})
    # Apple Pay и Google Pay — в самом конце, после звёзд (порядок владельца).
    return out + wallets


WALLETS = ("applepay", "googlepay")


def _day_start(request: web.Request, now: datetime | None = None) -> datetime:
    """Начало СЕГОДНЯ у пользователя, а не на сервере.

    «+2 сегодня» на экране приглашений считается от полуночи человека: сервер
    живёт в UTC, и у пользователя из Владивостока «сегодня» иначе начиналось
    бы в десять утра. Смещение — как отдаёт `Date.getTimezoneOffset()` (минуты,
    со знаком «UTC минус местное»); мусор и всё за пределами суток — ноль.
    """
    try:
        offset = int(request.query.get("tz", "0"))
    except ValueError:
        offset = 0
    if abs(offset) > 24 * 60:
        offset = 0
    now = now or datetime.now(timezone.utc)
    local = now - timedelta(minutes=offset)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=offset)


async def referrals(request: web.Request) -> web.Response:
    """Экран «Приглашения»: сводка за всё время и сегодня, список приглашённых."""
    cfg = request.app[CFG_KEY]
    user: WebAppUser = request[USER_KEY]
    if cfg.referral_percent <= 0:
        return _json_error(404, "referrals off")
    since = _day_start(request)
    try:
        days = int(request.query.get("days", "0"))
    except ValueError:
        days = 0
    window = since - timedelta(days=days - 1) if days > 0 else None
    stats = await repo.referral_stats(user.id, since=since)
    friends = await repo.referral_friends(user.id, since=window)
    return web.json_response(
        {
            "stats": stats,
            "friends": [
                {
                    # Только имя: ни @username, ни id. Пригласившему достаточно
                    # узнать своего, а чужой контакт ему отдавать незачем.
                    "name": f["name"] or "",
                    "joined_at": f["joined_at"].isoformat() if f["joined_at"] else None,
                    "earned": f["earned"],
                    "paid": f["paid"],
                }
                for f in friends
            ],
        }
    )


async def payments_history(request: web.Request) -> web.Response:
    """История в «Тарифах»: покупки и бонусы за приглашённых, свежие сверху."""
    user: WebAppUser = request[USER_KEY]
    paid = await repo.user_payments(user.id)
    bonuses = await repo.referral_bonuses(user.id)
    items = [
        {"kind": "payment", "at": p.created_at.isoformat(), "tokens": p.tokens, "stars": p.stars,
         "rub": p.rub}
        for p in paid
    ] + [{"kind": "bonus", "at": at.isoformat(), "share": share} for at, share in bonuses]
    items.sort(key=lambda x: x["at"], reverse=True)
    return web.json_response({"items": items[:50]})


async def upload_start(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    user: WebAppUser = request[USER_KEY]
    body = await request.json()
    size = int(body.get("size") or 0)
    if size <= 0:
        return await _fail(request, 400, "api.empty_file")
    if size > cfg.webapp_max_upload_mb * 1024 * 1024:
        return await _fail(request, 413, "api.too_big", mb=cfg.webapp_max_upload_mb)
    open_uploads = _sweep_stale_uploads(cfg)
    if sum(1 for _, meta in open_uploads if meta.get("user_id") == user.id) >= MAX_OPEN_UPLOADS:
        return await _fail(request, 429, "api.too_many_uploads", n=MAX_OPEN_UPLOADS)
    # Отказ до первого байта, а не на середине: пользователь с гигабайтным
    # файлом иначе узнаёт о нехватке места через десять минут загрузки.
    #
    # Из свободного места вычитается обещанное другим приёмам: их байты ещё не
    # пришли, и без этого вычета каждый новый приём получал бы одно и то же
    # место заново. Диск может кончиться и снаружи — машина общая, — тогда
    # запись упадёт по ходу загрузки; это уже не наш учёт, а чужой сосед.
    free = shutil.disk_usage(cfg.work_dir).free - DISK_RESERVE_MB * 1024 * 1024
    if size > free - _promised_bytes(open_uploads):
        return await _fail(request, 507, "api.no_space")

    upload_id = uuid.uuid4().hex
    folder = _upload_dir(cfg, upload_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "data").touch()
    (folder / "meta.json").write_text(
        json.dumps({"user_id": user.id, "size": size, "name": str(body.get("name") or "")[:200]}),
        encoding="utf-8",
    )
    return web.json_response(
        {"upload_id": upload_id, "chunk_bytes": cfg.webapp_chunk_mb * 1024 * 1024, "received": 0}
    )


async def _own_upload(
    request: web.Request, upload_id: str | None = None
) -> tuple[Path, dict[str, Any]] | web.Response:
    """Приём этого пользователя: из адреса или, для верхнего видео, из тела."""
    cfg = request.app[CFG_KEY]
    user: WebAppUser = request[USER_KEY]
    upload_id = upload_id if upload_id is not None else request.match_info["upload_id"]
    if not isinstance(upload_id, str) or not _UPLOAD_ID.match(upload_id):
        return await _fail(request, 404, "api.upload_not_found")
    folder = _upload_dir(cfg, upload_id)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return await _fail(request, 404, "api.upload_not_found")
    if meta.get("user_id") != user.id:
        return await _fail(request, 404, "api.upload_not_found")
    return folder, meta


async def upload_chunk(request: web.Request) -> web.Response:
    own = await _own_upload(request)
    if isinstance(own, web.Response):
        return own
    folder, meta = own
    data_path = folder / "data"
    have = data_path.stat().st_size
    try:
        offset = int(request.query.get("offset", "-1"))
    except ValueError:
        return await _fail(request, 400, "api.bad_offset")

    chunk = await request.read()
    end = offset + len(chunk)
    if end > meta["size"]:
        return await _fail(request, 400, "api.chunk_too_far")
    if end <= have:
        # Повтор уже принятого куска: ответ потерялся на мобильной сети, а байты дошли.
        return web.json_response({"received": have})
    if offset != have:
        # Клиент и сервер разошлись в том, сколько принято. Ответ говорит, откуда
        # продолжать, — клиент перешлёт с нужного места, а не с нуля.
        return await _fail(request, 409, "api.offset_mismatch", extra={"received": have})

    def append() -> None:
        with data_path.open("ab") as fh:
            fh.write(chunk)

    await asyncio.to_thread(append)
    await asyncio.to_thread(_renew_finished_uploads, request.app[CFG_KEY], meta["user_id"], folder)
    return web.json_response({"received": end})


async def upload_finish(request: web.Request) -> web.Response:
    """Файл целиком у нас → проверка → списание → очередь. Порядок важен.

    Проверка видео идёт ДО списания: не-видео и видео не той длины отбиваются,
    не трогая баланс. Всё, что может сломаться после списания (хранилище,
    очередь), возвращает ролик на баланс.
    """
    from src.bot.handlers.video import watch_job  # отложенно: хендлеры тянут весь бот
    from src.pipeline.probe import probe
    from src.worker.tasks import QUEUE_CPU, get_queue

    cfg = request.app[CFG_KEY]
    bot = request.app[BOT_KEY]
    own = await _own_upload(request)
    if isinstance(own, web.Response):
        return own
    folder, meta = own
    if (folder / "data").stat().st_size != meta["size"]:
        received = (folder / "data").stat().st_size
        return await _fail(request, 409, "api.incomplete", extra={"received": received})
    body = await request.json()
    # Трек «Музыки из рилса» отказов не порождает: монтаж мог быть запущен, пока
    # ссылка ещё проверялась, или папку трека уже убрали. Тогда ролик просто
    # без музыки, а приложение об этом скажет. Отказ здесь стоил бы человеку
    # повторной заливки видео ради музыки.
    want_music = body.get("music_id") is not None
    music_path = _own_music(cfg, request[USER_KEY].id, body.get("music_id")) if want_music else None
    # «Два видео»: верхний приём проверяется ДО того, как забран нижний. Чужой
    # или недогруженный верхний — отказ, а свой нижний цел, и его можно
    # отправить снова с правильным верхним.
    top: tuple[Path, dict[str, Any]] | None = None
    if body.get("top_upload_id") is not None:
        own_top = await _own_upload(request, body.get("top_upload_id"))
        if isinstance(own_top, web.Response):
            return own_top
        if own_top[0] == folder:
            return await _fail(request, 404, "api.upload_not_found")
        if (own_top[0] / "data").stat().st_size != own_top[1]["size"]:
            return await _fail(request, 409, "api.incomplete",
                               extra={"received": (own_top[0] / "data").stat().st_size})
        top = own_top
    # Забираем приём себе ДО всякой долгой работы: это и защита от двух «Готово»
    # разом (две задачи за один файл — два списанных ролика), и уход из-под
    # уборки просроченных заявок на время сборки.
    claimed = _claim_upload(folder)
    if claimed is None:
        return await _fail(request, 404, "api.upload_not_found")
    folder = claimed
    data_path = folder / "data"
    top_folder: Path | None = None
    if top is not None:
        top_folder = _claim_upload(top[0])
        if top_folder is None:
            shutil.rmtree(folder, ignore_errors=True)
            return await _fail(request, 404, "api.upload_not_found")
    # ВСЁ, что дальше, — под общей уборкой. Отказ после того, как приём забрали
    # (не та тема, нет баланса, очередь полна, упал ffprobe), оставлял папку
    # лежать до почасовой уборки, и в счёт обещанного места она уже не шла:
    # цикл «залил — получил отказ» кладёт на диск сколько угодно гигабайт.
    try:
        preset = str(body.get("preset") or "")
        if preset not in available():
            return await _fail(request, 400, "api.unknown_style")
        options = JobOptions.model_validate(body.get("options") or {})

        user = await _db_user(request)
        lang = lang_of(user)
        request[LANG_KEY] = lang
        limits = {"min_s": cfg.min_video_seconds, "max_m": cfg.max_video_minutes}
        if user.is_blocked:
            return await _fail(request, 403, "api.blocked")
        if not user.is_admin and await repo.active_jobs_count(user.id) >= 1:
            return await _fail(request, 429, "video.one_in_flight")
        if await repo.active_jobs_count() >= cfg.queue_max_size:
            return await _fail(request, 503, "video.queue_full")

        try:
            info = await asyncio.to_thread(probe, data_path)
        except Exception:  # noqa: BLE001 — ffprobe не прочитал: это не видео или битый файл
            return await _fail(request, 415, "api.unreadable")
        if info.duration < cfg.min_video_seconds:
            return _json_error(400, error_text("too_short", lang, **limits))
        if info.duration > cfg.max_video_seconds:
            return _json_error(400, error_text("too_long", lang, **limits))
        if not info.has_audio:
            return _json_error(400, error_text("no_audio", lang, **limits))
        if top_folder is not None:
            # Верхнему не нужны ни звук, ни пределы длины: он без звука, а
            # короткий просто раньше раскроет нижнее на весь экран. Но он
            # обязан быть ВИДЕО: картинка и обложка mp3 проходят ffprobe и
            # дали бы ролик без верха или с застывшим верхом.
            from src.pipeline.split import top_video_seconds

            try:
                await asyncio.to_thread(top_video_seconds, top_folder / "data")
            except Exception:  # noqa: BLE001 — не видео или ffprobe не прочитал
                return await _fail(request, 415, "api.top_unreadable")

        try:
            job = await repo.create_job(
                user.id,
                source_file_id=None,
                source_key=None,
                source_size=meta["size"],
                preset=preset,
                chat_id=user.id,
                options=options.model_dump(),
                charge=cfg.charges(is_admin=user.is_admin),
            )
        except repo.InsufficientBalance:
            return await _fail(request, 402, "api.no_balance", extra={"balance": user.balance})

        try:
            key = storage.key_for(SOURCES, str(job.id), "source.mp4")
            await storage.upload(data_path, key, content_type="video/mp4")
            await repo.update_job(job.id, source_key=key, source_duration=info.duration, preset=preset)
            if top_folder is not None:
                top_key = storage.key_for(SOURCES, str(job.id), "top.mp4")
                await storage.upload(top_folder / "data", top_key, content_type="video/mp4")
                await repo.update_job(job.id, top_source_key=top_key)
            music_attached = False
            if music_path is not None:
                try:
                    music_key = storage.key_for(SOURCES, str(job.id), "music" + music_path.suffix)
                    await storage.upload(music_path, music_key,
                                         content_type="audio/mp4" if music_path.suffix == ".m4a"
                                         else "application/octet-stream")
                    await repo.update_job(job.id, music_key=music_key)
                    music_attached = True
                except Exception as e:  # noqa: BLE001 — без музыки ролик всё равно едет
                    log.warning("задача %s: трек из рилса не залился: %s", job.id, e)
                finally:
                    shutil.rmtree(music_path.parent, ignore_errors=True)
            await repo.set_user_preset(user.id, preset)
            # Цвет и шрифт запоминаются между роликами, как и тема. Раньше
            # приложение каждый раз подставляло умолчание, и выбранная антиква
            # на следующем ролике молча становилась гротеском.
            await repo.set_user_settings(user.id, accent=options.accent, font=options.font)

            status_id: int | None = None
            try:
                status = await bot.send_message(user.id, t("video.accepted_app", lang))
                status_id = status.message_id
                await repo.update_job(job.id, status_message_id=status_id)
            except Exception as e:  # noqa: BLE001 — нет чата с ботом: прогресс увидят в приложении
                log.warning("задача %s: не смог написать в чат %s: %s", job.id, user.id, e)

            queue = await get_queue(QUEUE_CPU)
            try:
                await queue.enqueue_job("process_job", str(job.id), _queue_name=QUEUE_CPU)
            finally:
                await queue.close()
        except Exception as e:  # noqa: BLE001
            log.exception("задача %s: не поставилась в очередь", job.id)
            await repo.mark_failed(job.id, f"постановка в очередь: {e}")
            await repo.refund_job(job.id)
            return await _fail(request, 500, "api.enqueue_failed")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if top_folder is not None:
            shutil.rmtree(top_folder, ignore_errors=True)

    if status_id is not None:
        watch_job(bot, user.id, status_id, str(job.id), preset)
    fresh = await repo.get_job(job.id)
    view: dict[str, Any] = {
        "job": _job_view(fresh or job, live=None, secret=cfg.bot_token, lang=lang, cfg=cfg)
    }
    if want_music and not music_attached:
        view["music_dropped"] = True
    return web.json_response(view)


async def _live_progress(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    from src.worker.tasks import get_queue, progress_key

    if not job_ids:
        return {}
    redis = await get_queue()
    try:
        raws = await redis.mget([progress_key(j) for j in job_ids])
    finally:
        await redis.close()
    out: dict[str, dict[str, Any]] = {}
    for job_id, raw in zip(job_ids, raws):
        if raw:
            try:
                out[job_id] = json.loads(raw if isinstance(raw, str) else raw.decode())
            except ValueError:
                pass
    return out


async def jobs_list(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    user: WebAppUser = request[USER_KEY]
    jobs = await repo.user_jobs(user.id)
    active = [
        str(j.id) for j in jobs if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    ]
    live = await _live_progress(active)
    db_user = await repo.get_user(user.id)
    lang = lang_of(db_user) if db_user else lang_of(user)
    return web.json_response(
        {
            "jobs": [
                _job_view(j, live=live.get(str(j.id)), secret=cfg.bot_token, lang=lang, cfg=cfg)
                for j in jobs
            ],
            "balance": db_user.balance if db_user else 0,
        }
    )


async def job_send(request: web.Request) -> web.Response:
    """Прислать готовый ролик в чат ещё раз — если сообщение потерялось или удалено."""
    from src.bot.handlers.video import send_result

    user: WebAppUser = request[USER_KEY]
    job = await _own_job(request, user)
    if job is None:
        return await _fail(request, 404, "api.video_not_found")
    if job.files_purged_at is not None:
        return await _fail(request, 410, "api.purged")
    if job.status != JobStatus.DONE or not job.result_key:
        return await _fail(request, 409, "api.not_ready")
    try:
        await send_result(request.app[BOT_KEY], user.id, job)
    except Exception as e:  # noqa: BLE001
        log.warning("задача %s: повторная отправка не удалась: %s", job.id, e)
        return await _fail(request, 502, "api.resend_failed")
    return web.json_response({"ok": True})


async def _own_job(request: web.Request, user: WebAppUser) -> Job | None:
    try:
        job = await repo.get_job(request.match_info["job_id"])
    except ValueError:
        return None
    if job is None or job.user_id != user.id:
        return None
    return job


# ------------------------------------------------------------------ PayHot

PAYHOT_KEY = web.AppKey("payhot", object)
# Не больше стольких счетов на человека за окно: каждый — настоящий платёж в
# PayHot и работа для фонового опроса.
PAYHOT_ORDERS_PER_WINDOW = 5
PAYHOT_ORDER_WINDOW = timedelta(minutes=10)
# Заказ старше часа опрашивается раз в столько проходов (≈10 минут при 30 с).
PAYHOT_SLOW_EVERY = 20
# Замок на человека вокруг «посчитать заказы → записать новый». Без него
# десять запросов разом проходили проверку лимита до первой записи. Приложение
# — один процесс (служба reelbot-bot), поэтому замка в памяти достаточно.
PAYHOT_LOCKS_KEY = web.AppKey("payhot_locks", dict)


async def _payhot_return_url(request: web.Request) -> str:
    cfg = request.app[CFG_KEY]
    if cfg.payhot_return_url.strip():
        return cfg.payhot_return_url.strip()
    me = await request.app[BOT_KEY].me()
    return f"https://t.me/{me.username}"


async def payhot_order(request: web.Request) -> web.Response:
    """Пакет + способ → заказ у нас → счёт PayHot → ссылка оплаты.

    Сумма — только из пакета на сервере; клиент присылает лишь «какой пакет» и
    «каким способом». Оплату подтверждает `payhot_order_status` по API PayHot.
    """
    cfg = request.app[CFG_KEY]
    user: WebAppUser = request[USER_KEY]
    client = request.app.get(PAYHOT_KEY)
    db_user = await _db_user(request)
    request[LANG_KEY] = lang_of(db_user)
    if db_user.is_blocked:
        return await _fail(request, 403, "api.blocked")
    try:
        body = await request.json()
        tokens = int(body.get("tokens"))
    except (TypeError, ValueError, AttributeError):
        return await _fail(request, 400, "api.no_package")
    package = cfg.package(tokens)
    if package is None or not package.rub:
        return await _fail(request, 400, "api.unknown_package")
    method = payhot_mod.method(cfg, str(body.get("method") or ""))
    if method is None or client is None:
        return await _fail(request, 400, "api.unknown_method")
    if not method.allows_rub(package.rub):
        return await _fail(request, 400, "api.below_minimum", rub=method.min_amount_minor // 100)
    try:
        if method.currency == "EUR":
            amount = await client.euro_for_rubles(package.rub * 100, method=method.payhot_method)
            if method.min_amount_minor and amount < method.min_amount_minor:
                return await _fail(request, 400, "api.method_not_for_package")
        else:
            amount = package.rub * 100
        lock = request.app[PAYHOT_LOCKS_KEY].setdefault(user.id, asyncio.Lock())
        async with lock:
            since = datetime.now(timezone.utc) - PAYHOT_ORDER_WINDOW
            if await repo.count_payhot_orders(user.id, since=since) >= PAYHOT_ORDERS_PER_WINDOW:
                return await _fail(request, 429, "api.too_many_orders")
            order = await repo.create_payhot_order(
                user_id=user.id, tokens=package.tokens, rub=package.rub, method=method.key,
                currency=method.currency, amount_minor=amount,
            )
        created = await client.create_payment(
            order_ref=str(order.id), amount_minor=amount, currency=method.currency,
            method=method.payhot_method, customer_id=f"tg-{user.id}",
            success_url=await _payhot_return_url(request),
        )
    except payhot_mod.PayHotError as e:
        log.warning("payhot: счёт для %s не создан (%s, %s): %s", user.id, method.key, tokens, e)
        return await _fail(request, 502, "api.payhot_failed")
    await repo.set_payhot_payment(order.id, created.payment_id)
    log.info("payhot: заказ %s — %s, %d токенов, %d %s", order.id, method.key, package.tokens,
             amount, method.currency)
    return web.json_response({"order_id": str(order.id), "url": created.checkout_url})


async def payhot_order_status(request: web.Request) -> web.Response:
    """Статус заказа из PayHot; оплачен — токены зачисляются здесь же (один раз)."""
    from src.payments import orders

    user: WebAppUser = request[USER_KEY]
    raw = request.match_info["order_id"]
    try:
        uuid.UUID(raw)
    except ValueError:
        return await _fail(request, 404, "api.order_not_found")
    order = await repo.get_payhot_order(raw)
    if order is None or order.user_id != user.id:
        return await _fail(request, 404, "api.order_not_found")
    client = request.app.get(PAYHOT_KEY)
    if client is None:
        return web.json_response({"status": order.status, "final": True, "credited": False})
    got = await orders.settle(order.id, client)
    if got.credited:
        await _notify_payhot_credit(request.app[BOT_KEY], got)
    return web.json_response({"status": got.status, "final": got.final, "credited": got.credited})


async def _notify_payhot_credit(bot: Any, got: Any) -> None:
    """Сообщение в чат о зачислении. Деньги уже на месте — это только весть."""
    try:
        db_user = await repo.get_user(got.user_id)
        lang = lang_of(db_user) if db_user else "ru"
        await bot.send_message(got.user_id, t(
            "pay.credited", lang, tokens=tokens_text(got.tokens, lang),
            balance=tokens_text(db_user.balance if db_user else got.tokens, lang),
        ))
    except Exception as e:  # noqa: BLE001
        log.info("payhot: весть о зачислении %s не ушла: %s", got.user_id, e)


async def payhot_poll_once(app: web.Application, *, pass_no: int = 0) -> int:
    """Один проход фонового опроса. → сколько зачислено.

    Свежие заказы (до часа) — каждый проход; старше — раз в `PAYHOT_SLOW_EVERY`
    проходов, до `payhot_order_hours`: поздняя оплата должна дойти, но неделя
    брошенных счетов не должна растягивать каждый проход.
    """
    from src.payments import orders

    cfg = app[CFG_KEY]
    client = app.get(PAYHOT_KEY)
    if client is None:
        return 0
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=cfg.payhot_order_hours)
    slow_turn = pass_no % PAYHOT_SLOW_EVERY == 0
    credited = 0
    for order in await repo.payhot_orders_to_check(since=since):
        if now - order.created_at > timedelta(hours=1) and not slow_turn:
            continue
        try:
            got = await orders.settle(order.id, client)
        except Exception:  # noqa: BLE001 — один битый заказ не должен стопорить остальные
            log.exception("payhot: опрос заказа %s упал", order.id)
            continue
        if got.credited:
            credited += 1
            await _notify_payhot_credit(app[BOT_KEY], got)
    return credited


async def _payhot_lifecycle(app: web.Application):
    """Клиент PayHot и фоновый опрос живут столько же, сколько приложение."""
    cfg = app[CFG_KEY]
    task = None
    if PAYHOT_KEY not in app and cfg.payhot_api_key.strip():
        app[PAYHOT_KEY] = payhot_mod.PayHotClient.from_settings(cfg)
    if PAYHOT_KEY in app and cfg.payhot_poll_s > 0:
        async def loop() -> None:
            pass_no = 0
            while True:
                await asyncio.sleep(cfg.payhot_poll_s)
                try:
                    await payhot_poll_once(app, pass_no=pass_no)
                except Exception:  # noqa: BLE001
                    log.exception("payhot: проход опроса упал")
                pass_no += 1

        task = asyncio.create_task(loop())
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    client = app.get(PAYHOT_KEY)
    if client is not None and hasattr(client, "aclose"):
        await client.aclose()


async def invoice(request: web.Request) -> web.Response:
    cfg = request.app[CFG_KEY]
    bot = request.app[BOT_KEY]
    lang = lang_of(await _db_user(request))
    request[LANG_KEY] = lang
    body = await request.json()
    try:
        tokens = int(body.get("tokens"))
    except (TypeError, ValueError):
        return await _fail(request, 400, "api.no_package")
    package = cfg.package(tokens)
    if package is None:
        return await _fail(request, 400, "api.unknown_package")
    amount = tokens_text(tokens, lang)
    link = await bot.create_invoice_link(
        title=t("pay.invoice_title", lang, tokens=amount),
        description=t("pay.invoice_description", lang),
        payload=f"tokens:{tokens}",
        currency="XTR",
        prices=[LabeledPrice(label=t("pay.invoice_label", lang, tokens=amount), amount=package.stars)],
    )
    return web.json_response({"link": link})


async def set_language(request: web.Request) -> web.Response:
    """Смена языка из приложения. Тот же выбор, что кнопками в чате, — и кнопка меню тоже."""
    from src.bot.handlers.common import apply_menu_button

    body = await request.json()
    lang = str(body.get("lang") or "")
    if lang not in LANGS:
        return _json_error(400, "lang")
    user = await _db_user(request)
    await repo.set_user_lang(user.id, lang)
    await apply_menu_button(request.app[BOT_KEY], user.id, lang)
    return web.json_response({"lang": lang})


# ------------------------------------------------------------------ медиа и статика


def _browser_lang(request: web.Request) -> str:
    # У плеера и шрифта нет подписи Telegram, а значит и пользователя — только браузер.
    return "ru" if request.headers.get("Accept-Language", "").lower().startswith("ru") else "en"


async def media(request: web.Request) -> web.StreamResponse:
    """Готовый ролик для плеера в приложении. Файл кешируется на диске ради Range."""
    cfg = request.app[CFG_KEY]
    job_id = request.match_info["job_id"]
    if not check_media_token(job_id, request.query.get("t", ""), cfg.bot_token):
        return _json_error(403, t("api.link_expired", _browser_lang(request)))
    try:
        job = await repo.get_job(job_id)
    except ValueError:
        job = None
    if job is None or not job.result_key:
        return _json_error(404, t("api.video_not_found", _browser_lang(request)))
    cache = Path(cfg.work_dir) / "media-cache" / f"{job.id}.mp4"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        part = cache.with_suffix(".part")
        await storage.download(job.result_key, part)
        part.replace(cache)
    return web.FileResponse(cache, headers={"Cache-Control": "private, max-age=3600"})


@lru_cache(maxsize=32)
def _gzipped_font(path: Path, mtime_ns: int) -> bytes:
    """Сжатый шрифт, посчитанный один раз на файл.

    Приложение объявляет ВСЕ начертания библиотеки, и каждое из них рисуется
    на первом же экране — плитка темы её шрифтом, чип выбора образцом «Аа», —
    поэтому браузер честно тянет все восемнадцать. Это 3.1 МБ в TTF и 1.5 МБ в
    gzip: шрифт сжимается вдвое, и на мобильной сети это разница между «экран
    собрался» и «экран собирается».

    `mtime_ns` в ключе кеша не декорация: подменённый на диске файл обязан
    отдаться заново, а не остаться в памяти процесса до перезапуска.
    """
    del mtime_ns
    return gzip.compress(path.read_bytes(), 9)


async def font(request: web.Request) -> web.StreamResponse:
    """Шрифт темы для превью. Отдаём только файлы, которые назвал какой-то пресет."""
    cfg = request.app[CFG_KEY]
    name = request.match_info["name"]
    # Разрешены и шрифты тем, и вся библиотека выбора: раньше список собирался
    # только из пресетов, и шрифт, которого не назвала ни одна тема, браузер бы
    # не получил — чип выбора показывал бы превью чужим шрифтом.
    allowed = {choice.file for _, choice in FONTS.values() if choice}
    for preset in available():
        try:
            loaded = load_preset(preset)
        except Exception:  # noqa: BLE001
            continue
        allowed.add(loaded.font.file)
        if loaded.accent_font:
            allowed.add(loaded.accent_font.file)
    path = Path(cfg.fonts_dir) / name
    if name not in allowed or not path.is_file():
        return _json_error(404, t("api.font_not_found", _browser_lang(request)))
    headers = {"Cache-Control": "public, max-age=86400", "Vary": "Accept-Encoding"}
    # `gzip;q=0` — это явный ОТКАЗ от сжатия, а не просьба о нём: подстрока
    # «gzip» в заголовке есть в обоих случаях.
    accept = request.headers.get("Accept-Encoding", "").lower().replace(" ", "")
    if "gzip" in accept and "gzip;q=0" not in accept:
        # В отдельном потоке: это 70-80 мс чистого процессора на файл, а цикл
        # событий здесь общий с long-polling бота (`src/bot/main.py`). Первое
        # открытие приложения после выкладки тянет все восемнадцать начертаний
        # разом — полсекунды, на которые бот переставал бы отвечать всем.
        body = await asyncio.to_thread(_gzipped_font, path, path.stat().st_mtime_ns)
        return web.Response(
            body=body,
            headers={**headers, "Content-Encoding": "gzip"},
            content_type="font/ttf",
        )
    return web.FileResponse(path, headers=headers)


async def index(request: web.Request) -> web.StreamResponse:
    # Без кеша: адрес туннеля постоянный в рамках запуска, а приложение правится
    # чаще, чем Telegram сбрасывает свой кеш WebView.
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@web.middleware
async def no_cache_static(request: web.Request, handler):
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def create_app(bot: Bot, cfg: Settings) -> web.Application:
    # Кусок загрузки плюс запас на заголовки; целиком файл сюда не приходит никогда.
    app = web.Application(
        middlewares=[auth_middleware, no_cache_static],
        client_max_size=(cfg.webapp_chunk_mb + 1) * 1024 * 1024,
    )
    app[BOT_KEY] = bot
    app[CFG_KEY] = cfg
    app[MUSIC_CHECKS_KEY] = {}
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/media/{job_id}.mp4", media)
    app.router.add_get("/fonts/{name}", font)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/uploads", upload_start)
    app.router.add_put("/api/uploads/{upload_id}", upload_chunk)
    app.router.add_post("/api/uploads/{upload_id}/finish", upload_finish)
    app.router.add_post("/api/music", music_check)
    app.router.add_get("/api/jobs", jobs_list)
    app.router.add_post("/api/jobs/{job_id}/send", job_send)
    app.router.add_post("/api/invoice", invoice)
    app[PAYHOT_LOCKS_KEY] = {}
    app.router.add_post("/api/payhot/orders", payhot_order)
    app.router.add_get("/api/payhot/orders/{order_id}", payhot_order_status)
    app.cleanup_ctx.append(_payhot_lifecycle)
    app.router.add_post("/api/language", set_language)
    app.router.add_get("/api/referrals", referrals)
    app.router.add_get("/api/payments", payments_history)
    return app


async def start_server(bot: Bot, cfg: Settings) -> web.AppRunner:
    runner = web.AppRunner(create_app(bot, cfg), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webapp_host, cfg.webapp_port)
    await site.start()
    log.info("Mini App слушает http://%s:%d", cfg.webapp_host, cfg.webapp_port)
    return runner
