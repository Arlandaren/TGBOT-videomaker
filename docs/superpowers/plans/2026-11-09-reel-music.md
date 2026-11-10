# Музыка из рилса — план работ

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Человек вставляет в Mini App ссылку на рилс Instagram, и звук этого рилса тихо играет фоном под его речью в готовом ролике.

**Architecture:** Новый модуль `src/pipeline/reel_audio.py` разбирает ссылку и запускает yt-dlp отдельным процессом. Веб-приложение проверяет ссылку сразу (`POST /api/music`) и кладёт трек в папку `music-<id>` в `work_dir`. `finish` переносит трек в хранилище при задаче (`Job.music_key`). Воркер скачивает трек и передаёт его в `build_audio(music_file=…)`, где трек выравнивается по громкости и приглушается под голос существующим дакингом. Перерендер начинает сводить звук так же, как первый проход.

**Tech Stack:** Python 3.12, aiohttp, arq, SQLAlchemy + alembic, ffmpeg, yt-dlp (подпроцесс), httpx, ванильный JS Mini App.

**Spec:** `docs/superpowers/specs/2026-11-09-reel-music-design.md`

## Global Constraints

- yt-dlp закреплён в `pyproject.toml`: `"yt-dlp==2026.10.26"`. Автообновления нет.
- yt-dlp получает только ссылку, собранную из разобранного кода: `https://www.instagram.com/<reel|p|tv>/<код>/`. Исходная строка пользователя в yt-dlp не попадает никогда.
- Хосты: `instagram.com`, `www.instagram.com`, `m.instagram.com`, `instagr.am`. Схема `http` переписывается в `https`, остальные схемы — отказ.
- Короткие ссылки `/share/…` раскрываются одним GET без автопереходов, по ссылке, собранной из кода на `https://www.instagram.com`. `Location` проходит те же правила.
- Пределы: таймаут yt-dlp 45 с (`REEL_FETCH_TIMEOUT_S`), файл не больше 25 МБ (`REEL_MAX_BYTES`), одновременно не больше 2 скачиваний на сервер (`MAX_PARALLEL_FETCHES`), не больше 6 проверок в минуту на человека (`MUSIC_CHECKS_PER_MINUTE`).
- Громкость трека из рилса: `loudnorm` к `target_lufs − 16` (`REEL_MUSIC_BELOW_VOICE_DB = 16.0`), затем существующий дакинг `duck_db` пресета. Подложка из `assets/music` сводится как раньше.
- Музыка не роняет ролик: любая беда с треком на этапе монтажа даёт ролик без музыки и строку в подписи к ролику.
- Без ссылки первый проход ведёт себя как до этой работы. Намеренно меняется только перерендер: теперь он сводит звук.
- `server.py`, `app.js`, `index.html`, `app.css` в основном дереве содержат чужие незакоммиченные ханки. Работа идёт в worktree `/home/arslan/worktrees/tgbot-video-reel-music`, и коммитятся только свои изменения.
- Коды отказов и ключи текстов: `music.bad_link`, `music.no_audio`, `music.unavailable`, `music.instagram_down`, `music.too_big`, `music.too_many`, `music.dropped` (для ответа `finish`), `video.caption_music_lost` (подпись к ролику). Тексты на ru и en.

## Review Focus

- Ссылка вставлена вместе с текстом («Смотри https://www.instagram.com/reel/…»): нужно найти ссылку в строке и принять её. Тест в Task 1.
- Рилс без звуковой дорожки: человек должен увидеть «В этом рилсе нет звука», а не «Instagram не ответил». Тест в Task 1.
- Человек нажал «Смонтировать», пока ссылка ещё проверяется, или прошло больше двух часов и папку трека уже убрали: ролик собирается без музыки, ответ `finish` несёт `music_dropped: true`, приложение говорит об этом. Отказа и повторной заливки видео быть не должно. Тесты в Task 5.
- Трек длиннее ролика на минуты или короче секунды: обрезается или идёт по кругу, длина дорожки равна длине ролика. Тест в Task 2.
- Трек из хранилища не скачался в воркере: ролик готов, без музыки, в подписи строка `video.caption_music_lost`. Тест в Task 3.

---

### Task 1: Разбор ссылки и скачивание звука

**Files:**
- Create: `src/pipeline/reel_audio.py`
- Create: `tests/test_reel_audio.py`
- Modify: `pyproject.toml` (зависимость `"yt-dlp==2026.10.26"` в блок `# storage` или новый `# музыка из рилса`)

**Interfaces:**
- Produces:
  - `class ReelAudioError(Exception)` с атрибутом `code: str`, одно из `"bad_link" | "no_audio" | "unavailable" | "instagram_down" | "too_big"`.
  - `@dataclass(frozen=True) class ReelLink: kind: Literal["media", "share"]; url: str`. Для `media` `url` — каноническая ссылка `https://www.instagram.com/{reel|p|tv}/<код>/` (`/reels/` → `reel`). Для `share` — `https://www.instagram.com/share/<хвост>/`.
  - `parse_reel_link(raw: str) -> ReelLink | None` — чистая функция. Ищет первую ссылку Instagram в строке.
  - `async resolve_share(link: ReelLink, *, client: httpx.AsyncClient | None = None) -> ReelLink` — один GET с `follow_redirects=False`, таймаут 10 с; `Location` → `parse_reel_link` → обязан дать `kind == "media"`, иначе `ReelAudioError("bad_link")`. Сетевые ошибки → `ReelAudioError("instagram_down")`.
  - `@dataclass(frozen=True) class ReelTrack: path: Path; duration: float`
  - `async fetch_reel_audio(url: str, dst_dir: Path) -> ReelTrack` — `url` уже канонический (`kind == "media"`).
  - Модульные константы `YT_DLP: list[str] = [sys.executable, "-m", "yt_dlp"]` (тесты подменяют), `REEL_FETCH_TIMEOUT_S = 45`, `REEL_MAX_BYTES = 25 * 1024 * 1024`.

- [ ] **Step 1: Write the failing tests** в `tests/test_reel_audio.py`:

```python
@pytest.mark.parametrize("raw, want", [
    ("https://www.instagram.com/reel/DfXkZV0Sbuu/?utm_source=ig_web_copy_link&mdxt=NTc4", "https://www.instagram.com/reel/DfXkZV0Sbuu/"),
    ("https://instagram.com/reels/Cop84x6u7CP", "https://www.instagram.com/reel/Cop84x6u7CP/"),
    ("http://m.instagram.com/p/BQ0eAlwhDrw/?igsh=abc", "https://www.instagram.com/p/BQ0eAlwhDrw/"),
    ("https://www.instagram.com/marvelskies.fc/reel/CWqAgUZgCku/", "https://www.instagram.com/reel/CWqAgUZgCku/"),
    ("https://instagr.am/tv/BkfuX9UB-eK", "https://www.instagram.com/tv/BkfuX9UB-eK/"),
    ("Смотри какой звук https://www.instagram.com/reel/DfXkZV0Sbuu/ огонь", "https://www.instagram.com/reel/DfXkZV0Sbuu/"),
])
def test_a_reel_link_becomes_canonical(raw, want):
    assert parse_reel_link(raw) == ReelLink("media", want)

def test_a_share_link_is_kept_for_resolving():
    assert parse_reel_link("https://www.instagram.com/share/reel/BAabc123/") == ReelLink("share", "https://www.instagram.com/share/reel/BAabc123/")

@pytest.mark.parametrize("raw", [
    "", "просто текст", "https://www.tiktok.com/@a/video/1",
    "https://evil.com/reel/DfXkZV0Sbuu/", "https://instagram.com.evil.com/reel/X/",
    "https://www.instagram.com/someuser/", "file:///etc/passwd",
    "ftp://www.instagram.com/reel/DfXkZV0Sbuu/", "https://www.instagram.com/reel/../../x/",
])
def test_anything_else_is_not_a_reel(raw):
    assert parse_reel_link(raw) is None
```

`resolve_share` — через `httpx.MockTransport`:
- `Location: https://www.instagram.com/reel/DfXkZV0Sbuu/?igsh=x` → `ReelLink("media", "https://www.instagram.com/reel/DfXkZV0Sbuu/")`; транспорт проверяет, что запрос ушёл ровно на `https://www.instagram.com/share/reel/BAabc123/`.
- `Location: https://evil.com/reel/X/` → `ReelAudioError` с `code == "bad_link"`.
- Ответ 200 без `Location` → `bad_link`.
- Транспорт бросает `httpx.ConnectError` → `instagram_down`.

`fetch_reel_audio` — `monkeypatch.setattr(reel_audio, "YT_DLP", [sys.executable, str(fake)])`, где `fake` — скрипт в `tmp_path`, который по переменной окружения (`FAKE_MODE`) ведёт себя так:
- `ok`: находит в argv значение `-o`, пишет туда (`%(ext)s` → `m4a`) копию настоящего аудиофайла из ffmpeg (`sine` 3 с, AAC), выход 0 → `ReelTrack`, `duration ≈ 3.0` (±0.1), файл внутри `dst_dir`.
- `ok101`: то же, но выход 101 (сработал `--max-downloads 1`) → тоже успех.
- `noformat`: печатает в stderr `ERROR: [Instagram] X: Requested format is not available` и выходит 1 → `no_audio`.
- `video_only`: пишет файл с одной видеодорожкой (ffmpeg `testsrc` 1 с, `-an`) → `no_audio`.
- `private`: stderr `ERROR: [Instagram] X: This content may be inappropriate… login required` → `unavailable`; второй вариант — `HTTP Error 404: Not Found` → `unavailable`.
- `garbage`: stderr `ERROR: Unable to extract data` → `instagram_down`, и в логе есть запись с текстом `reel_audio_broken` (`caplog`).
- `sleep`: `time.sleep(5)` при `REEL_FETCH_TIMEOUT_S`, подменённом на 0.5 → `instagram_down`, процесс убит (проверить, что после ошибки в `dst_dir` нет файлов `*.part`).
- `toobig`: stderr `File is larger than max-filesize`, выход 0, файла нет → `too_big`.
- Отдельный тест: argv, который получил фейк (скрипт пишет его в JSON-файл), содержит `--ignore-config`, `--no-cache-dir`, `--max-filesize`, `--max-downloads`, `1`, `-f`, и последний аргумент равен переданному каноническому `url`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reel_audio.py -q`
Expected: FAIL — `ModuleNotFoundError: src.pipeline.reel_audio`

- [ ] **Step 3: Implement `src/pipeline/reel_audio.py`**

- Разбор: регулярка `https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/[^\s]+` находит кандидата. Дальше `urllib.parse.urlsplit`, точное сравнение `hostname` со списком. Путь без пустых сегментов: `[<автор>/]<reel|reels|p|tv>/<код>` или `share/[reel/|p/]<код>`, где код — `[A-Za-z0-9_-]{5,64}`. Запрос и фрагмент отбрасываются.
- yt-dlp argv: `[*YT_DLP, "--ignore-config", "--no-cache-dir", "--no-progress", "--no-warnings", "-q", "--socket-timeout", "15", "--max-filesize", str(REEL_MAX_BYTES), "--playlist-items", "1:10", "--max-downloads", "1", "-f", "ba/b[acodec!=none]", "--no-part", "-o", str(dst_dir / "track.%(ext)s"), url]`. Запуск через `asyncio.create_subprocess_exec`, stdout в DEVNULL, stderr в PIPE. При таймауте `proc.kill()` и `await proc.wait()`.
- Разбор stderr по подстрокам, без учёта регистра: `requested format is not available` → `no_audio`; `larger than max-filesize` → `too_big`; `login`, `private`, `404`, `not available`, `does not exist`, `restricted` → `unavailable`; всё прочее или код выхода не 0 и не 101 без файла → `instagram_down` плюс `log.warning("reel_audio_broken: %s", stderr_tail)`.
- После успеха: первый файл `track.*` в `dst_dir`; `probe()` из `src.pipeline.probe` в `asyncio.to_thread`; нет аудио или `probe` упал → `no_audio`. Длительность берётся из `MediaInfo.duration`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reel_audio.py -q`
Expected: PASS

- [ ] **Step 5: Install yt-dlp locally and check the real thing once**

Run: `.venv/bin/pip install yt-dlp==2026.10.26` (или `uv pip install`, чем ставит проект), затем
`python -c "import asyncio,tempfile,pathlib;from src.pipeline.reel_audio import *;print(asyncio.run(fetch_reel_audio('https://www.instagram.com/reel/DfXkZV0Sbuu/', pathlib.Path(tempfile.mkdtemp()))))"`
Expected: `ReelTrack(path=…/track.m4a, duration=36.1…)`. Если с этой машины Instagram не отдаёт, то же с сервера. В тесты сеть не попадает.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/pipeline/reel_audio.py tests/test_reel_audio.py
git commit -m "Звук рилса по ссылке: разбор ссылки и скачивание через yt-dlp"
```

---

### Task 2: Трек задачи в сведении звука

**Files:**
- Modify: `src/core/edl.py` (`Music`)
- Modify: `src/pipeline/audio_post.py` (`resolve_music`, `build_audio`)
- Test: `tests/test_audio_post.py`

**Interfaces:**
- Produces:
  - `Music.source: Literal["asset", "reel"] = "asset"`. Для `"reel"` поле `track` всегда равно `"reel"`, это не имя файла.
  - `resolve_music(edl, *, preset, cfg=None, music_file: Path | None = None) -> tuple[Path, Music] | None`. Если `music_file` передан и читается (`_audio_duration` не None) → `(music_file, Music(source="reel", track="reel", gain_db=0.0, duck_db=preset.audio.duck_db))`. Если не читается → `None` с предупреждением. Если `music_file is None`, а `edl.music.source == "reel"` → `None` без поиска в `assets/music`. Иначе как раньше.
  - `build_audio(..., music_file: Path | None = None)`: передаёт дальше. Для `source == "reel"` цепочка подложки — `AFORMAT, atrim, asetpts, loudnorm=I={target - REEL_MUSIC_BELOW_VOICE_DB}:TP=-6:LRA=11, AFORMAT` вместо `volume`. Фейды и дакинг — как у ассета.
  - `AudioTrack.music` для трека из рилса равно `"reel"`.
  - Константа `REEL_MUSIC_BELOW_VOICE_DB = 16.0` в `audio_post.py`.

- [ ] **Step 1: Write the failing tests** (рядом с музыкальными тестами, на фикстурах `voice_video`, `silent_video`):
  - `test_a_reel_track_lands_in_the_edl_as_reel`: `build_audio(simple_edl(), voice_video, …, music_file=music_file)` → `track.music == "reel"`, `edl.music.source == "reel"`, `edl.music.track == "reel"`.
  - `test_a_reel_track_is_levelled_whatever_its_loudness`: два трека из `sine`, один на `volume=-30dB`, другой на `volume=0dB`, оба 2.5 с; сведение каждого на `silent_video` → интегральная громкость результатов (`measure_loudness`/`ebur128`) отличается меньше чем на 3 LU и лежит в пределах ±3 LU от `target − 16`.
  - `test_a_reel_track_covers_the_whole_reel`: трек 0.6 с на `silent_video` 6 с → дорожка 6.0 ± 0.05 с, и в окне 4–5 с RMS музыки выше −60 дБ (трек пошёл по кругу). Трек 120 с → дорожка тоже 6.0 ± 0.05 с.
  - `test_an_unreadable_reel_track_costs_only_the_music`: `music_file` — файл с мусорными байтами → дорожка собрана, `track.music is None`, `edl.music is None`.
  - `test_a_reel_sheet_without_its_file_plays_without_music`: `edl.music = Music(source="reel", track="reel")`, `music_file=None`, в `cfg.music_dir` лежит файл с именем `reel.wav` → `track.music is None` (в ассетах не искали).
  - `test_an_old_sheet_still_reads`: `Music.model_validate({"track": "x.mp3", "gain_db": -20, "duck_db": -12}).source == "asset"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_audio_post.py -q -k "reel or old_sheet"`
Expected: FAIL — `unexpected keyword argument 'music_file'` / нет поля `source`

- [ ] **Step 3: Implement** по Interfaces. В комментарии к `Music.track` написать, что для `source="reel"` это метка, а файл приходит от задачи (`Job.music_key`): так EDL не несёт машинно-локальных путей.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_audio_post.py tests/test_edl.py -q`
Expected: PASS, включая старые музыкальные тесты

- [ ] **Step 5: Commit**

```bash
git add src/core/edl.py src/pipeline/audio_post.py tests/test_audio_post.py
git commit -m "Сведение: трек задачи из рилса, выровненный по громкости под голос"
```

---

### Task 3: Трек при задаче и в воркере

**Files:**
- Modify: `src/db/models.py` (`Job.music_key`), `src/db/repo.py` (`mark_files_purged`)
- Create: `alembic/versions/7a2e4c9b1d3f_jobs_music_key.py` (down_revision `'3f1c8a9d2b6e'`)
- Modify: `src/worker/tasks.py` (`fetch_music`, `process_job`, `purge_expired_files`)
- Modify: `src/pipeline/job.py` (`process_video`, `process_from_edl`: параметр `music_file`)
- Modify: `src/bot/handlers/video.py` (`_caption`)
- Modify: `src/core/i18n.py` (`video.caption_music_lost`)
- Test: `tests/test_money.py`, `tests/test_retention.py`, `tests/test_worker_loop.py`, `tests/test_delivery.py` (там, где уже проверяется подпись; иначе `tests/test_bot_access.py`)

**Interfaces:**
- Consumes: `build_audio(..., music_file=…)` из Task 2.
- Produces:
  - `Job.music_key: Mapped[str | None] = mapped_column(String(512))`.
  - `async fetch_music(job: Any, dst_dir: Path) -> Path | None` в `tasks.py`: ключа нет → `None`. Скачивание упало (любое исключение) → `log.warning` и `None`. Имя файла — `dst_dir / ("music" + суффикс ключа)`.
  - `process_video(..., music_file: Path | None = None)` и `process_from_edl(..., music_file: Path | None = None)` передают его в `build_audio`.
  - `video.caption_music_lost` ru: «Музыку из рилса подложить не удалось — ролик без неё.», en: «Couldn't add the reel's music — the video has none.»
  - `_caption` добавляет эту строку отдельной строкой, когда `job.music_key` есть, а `(job.edl or {}).get("music", {}) or {}` не содержит `source == "reel"`.

- [ ] **Step 1: Write the failing tests**
  - `test_money.py::test_the_music_track_is_kept_and_purged_with_the_job` — по образцу `test_the_top_source_is_kept_and_purged_with_the_job`: `music_key="sources/x/music.m4a"` сохраняется, после `mark_files_purged` равен `None`.
  - `test_retention.py`: задача с `music_key` → `purge_expired_files` удаляет и этот ключ (по образцу теста для `top_source_key`).
  - `test_worker_loop.py`: (а) задача с `music_key` → `process_video` получил `music_file`, который существует и содержит байты из хранилища; (а2) то же для задачи «Два видео» (`top_source_key` и `music_key` разом) → `process_video` получил и `top_src`, и `music_file`; (б) `storage.download` для музыки бросает `FileNotFoundError` → задача DONE, `music_file is None`; (в) задача без `music_key` → `music_file is None`.
  - Подпись: задача с `music_key` и `edl={"music": None, …}` → в `_caption` есть текст `video.caption_music_lost`. С `edl={"music": {"source": "reel", "track": "reel", …}}` — нет. Без `music_key` — нет.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_money.py tests/test_retention.py tests/test_worker_loop.py tests/test_delivery.py -q -k "music"`
Expected: FAIL — нет столбца `music_key` / параметра `music_file`

- [ ] **Step 3: Implement** по Interfaces. Миграция: `op.add_column('jobs', sa.Column('music_key', sa.String(length=512), nullable=True))` и обратная. В `purge_expired_files` добавить `getattr(job, "music_key", None)` в набор ключей.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_money.py tests/test_retention.py tests/test_worker_loop.py tests/test_delivery.py -q`
Expected: PASS. `test_money` требует локальный Postgres на 5439, без него пропускается. Миграцию прогнать на локальной базе: `alembic upgrade head`, затем `alembic downgrade -1` и снова `alembic upgrade head`.

- [ ] **Step 5: Commit**

```bash
git add src/db/models.py src/db/repo.py alembic/versions/7a2e4c9b1d3f_jobs_music_key.py src/worker/tasks.py src/pipeline/job.py src/bot/handlers/video.py src/core/i18n.py tests/
git commit -m "Трек из рилса хранится при задаче и доходит до сведения"
```

---

### Task 4: Перерендер сводит звук

**Files:**
- Modify: `src/worker/tasks.py` (`rerender_job`)
- Test: `tests/test_worker_loop.py`

**Interfaces:**
- Consumes: `fetch_music` (Task 3), `build_audio(..., music_file=…)` (Task 2).
- Produces: `rerender_job` после нарезки при `settings.audio_post_enabled` зовёт `build_audio(edl, cut_path, work / "audio.wav", preset=style, apply_cuts=options.cut, sfx=options.sfx, cfg=settings, music_file=await fetch_music(job, work))` в `asyncio.to_thread`. Путь дорожки уходит в `build_plan(..., audio_path=…)`. Исключение сведения → `log.warning`, ролик едет со звуком из `cut.mp4`, как в `process_video`.

- [ ] **Step 1: Write the failing tests** (по образцу `test_rerender_of_a_split_job_reuses_the_saved_switch`, с подменёнными тяжёлыми этапами):
  - `test_the_rerender_mixes_the_sound_like_the_first_pass`: `build_audio` вызван один раз с `apply_cuts == options.cut` и `sfx == options.sfx`, а `build_plan` получил `audio_path`, равный пути из возвращённого `AudioTrack`.
  - `test_the_rerender_keeps_the_reel_music`: задача с `music_key` → `build_audio` получил существующий `music_file`.
  - `test_a_failed_mix_does_not_fail_the_rerender`: `build_audio` бросает `RuntimeError` → рендер вызван с `audio_path is None`, задача перерендера вернула `result_key`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker_loop.py -q -k rerender`
Expected: FAIL — `build_audio` не вызывался

- [ ] **Step 3: Implement** по Interfaces.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker_loop.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/worker/tasks.py tests/test_worker_loop.py
git commit -m "Перерендер сводит звук, как первый проход: громкость, эффекты, музыка"
```

---

### Task 5: API проверки ссылки и привязка трека к задаче

**Files:**
- Modify: `src/core/config.py` (`MUSIC_PREFIX = "music-"` рядом с `UPLOAD_PREFIX`, с комментарием о договоре двух служб)
- Modify: `src/worker/tasks.py` (`cleanup_old_files`: `MUSIC_PREFIX` убирается по короткому сроку, как приёмы)
- Modify: `src/webapp/server.py` (`music_check`, маршрут, `upload_finish`)
- Modify: `src/core/i18n.py` (ключи `music.*`)
- Test: `tests/test_webapp_server.py`, `tests/test_retention.py` или тот файл, где тестируется `cleanup_old_files`

**Interfaces:**
- Consumes: `parse_reel_link`, `resolve_share`, `fetch_reel_audio`, `ReelAudioError` (Task 1); `Job.music_key` (Task 3).
- Produces:
  - `POST /api/music`, тело `{"url": str}`. Ответ 200 `{"music_id": "<32 hex>", "duration": float}`. Отказы: 400 `music.bad_link`, 422 `music.no_audio`, 422 `music.unavailable`, 502 `music.instagram_down`, 413 `music.too_big`, 429 `music.too_many`. Все через `_fail`.
  - Папка трека: `Path(cfg.work_dir) / f"{MUSIC_PREFIX}{music_id}"`, в ней `meta.json` `{"user_id": …, "duration": …}` и `track.<ext>`. При отказе папка удаляется.
  - Ограничения: Redis `INCR music-checks:<user_id>` с `EXPIRE 60`, больше `MUSIC_CHECKS_PER_MINUTE = 6` → 429. Модульный `asyncio.Semaphore(MAX_PARALLEL_FETCHES = 2)` вокруг скачивания.
  - `finish` принимает `music_id` (необязательно). Свой, существующий, с файлом `track.*` → после `create_job` и заливки исходников файл уходит в `storage.key_for(SOURCES, job.id, "music" + суффикс)`, затем `repo.update_job(job.id, music_key=…)`, папка удаляется. Чужой, неизвестный, неверного вида или исчезнувший → задача создаётся без музыки, в ответе `"music_dropped": true`. Ошибка заливки трека → `log.warning`, задача без музыки, `"music_dropped": true`, без возврата денег и без отказа.
  - Тексты `music.*` (ru / en):
    - `bad_link`: «Это не ссылка на рилс Instagram» / «That's not an Instagram reel link»
    - `no_audio`: «В этом рилсе нет звука» / «This reel has no sound»
    - `unavailable`: «Рилс закрыт или удалён» / «The reel is private or deleted»
    - `instagram_down`: «Instagram не ответил, попробуйте ещё раз» / «Instagram didn't respond, try again»
    - `too_big`: «Звук в этом рилсе слишком большой» / «This reel's sound is too large»
    - `too_many`: «Слишком много ссылок подряд, подождите минуту» / «Too many links in a row, wait a minute»
    - `dropped`: «Музыка не успела подготовиться — ролик будет без неё» / «The music wasn't ready — the video will have none»

- [ ] **Step 1: Write the failing tests** в `tests/test_webapp_server.py`. `fetch_reel_audio` и `resolve_share` подменяются через `monkeypatch` модуля `src.pipeline.reel_audio`, Redis — тем же двойником, что уже используют тесты сервера (или словарём через подмену `get_queue`):
  - `test_a_reel_link_gives_a_music_id`: ответ 200, `duration` из подменённого `ReelTrack`, папка `music-<id>` есть, `meta.json` с `user_id == 42`.
  - `test_a_non_instagram_link_is_refused_without_downloading`: `https://evil.com/reel/X/` → 400, подменённый `fetch_reel_audio` не вызывался.
  - `test_each_reel_failure_has_its_own_message`: параметризация `no_audio → 422`, `unavailable → 422`, `instagram_down → 502`, `too_big → 413`. Текст ответа совпадает с `t("music.<code>", "ru")`, папки `music-*` не осталось.
  - `test_the_seventh_check_in_a_minute_is_refused`: 6 запросов → 200, седьмой → 429.
  - `test_finish_attaches_the_music_to_the_job` (по образцу успешного `finish` с `finish_env`, где `create_job` возвращает задачу): `repo.update_job` получил `music_key`, оканчивающийся на `/music.m4a`, `storage.upload` вызван с файлом трека, папка `music-<id>` удалена.
  - `test_a_foreign_or_missing_music_id_only_drops_the_music`: чужой `music_id` → 200, `music_dropped is True`, задача создана, `music_key` не выставлен. То же для несуществующего id и для `"../x"`.
  - `cleanup_old_files`: папка `music-…` с `mtime` 3 часа назад удалена, с `mtime` 1 час назад — цела.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_webapp_server.py -q -k music`
Expected: FAIL — 404 на `/api/music`

- [ ] **Step 3: Implement** по Interfaces. Порядок в `music_check`: лимит → `parse_reel_link` → при `share` `resolve_share` → семафор → `fetch_reel_audio` в свежую папку. В `upload_finish` `music_id` читается из тела до создания задачи, но отказов не порождает.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_webapp_server.py tests/test_retention.py tests/test_i18n.py -q`
Expected: PASS (`test_i18n` проверяет, что у каждого ключа есть оба языка)

- [ ] **Step 5: Commit** — только свои ханки `server.py` (в worktree чужих нет).

```bash
git add src/core/config.py src/worker/tasks.py src/webapp/server.py src/core/i18n.py tests/
git commit -m "API: проверка ссылки на рилс и привязка трека к задаче"
```

---

### Task 6: Поле «Музыка из рилса» в Mini App

**Files:**
- Modify: `src/webapp/static/index.html` (шаг 2, новая карточка между блоком «Эффекты» и кнопкой `#cta`)
- Modify: `src/webapp/static/app.js` (состояние, проверка, тело `finish`, тексты)
- Modify: `src/webapp/static/app.css`

**Interfaces:**
- Consumes: `POST /api/music`, `music_id` и `music_dropped` в `finish` (Task 5).
- Produces:
  - Разметка: `<div class="section-head"><h2 data-i18n="music.title">Музыка из рилса</h2></div>`, карточка `#music-card` с `<input id="music-input" type="url" inputmode="url" autocomplete="off" data-i18n-placeholder="music.placeholder">`, кнопкой `#music-paste` (иконка, `data-i18n-aria="music.paste"`), строкой состояния `#music-status` и крестиком `#music-clear`.
  - `state.music = { status: "empty" | "checking" | "ready" | "error", id: string | null, duration: number | null, error: string | null, seq: number }`.
  - Проверка запускается на `paste` и на `change`, а на `input` — с паузой 600 мс. Ответ старее текущего `seq` отбрасывается.
  - Состояния строки: `checking` — крутилка и «Достаём звук…» (`music.checking`); `ready` — галочка и «Трек 0:36» (`music.ready`, `{t}` = `m:ss`); `error` — текст ошибки сервера, поле остаётся редактируемым.
  - `uploadAndStart` добавляет `body.music_id = state.music.id` только при `status === "ready"`. Если ответ несёт `music_dropped`, показать тост `music.dropped`.
  - Кнопка «Вставить» вызывает `navigator.clipboard.readText()` в `try`; при отказе фокусирует поле. Telegram WebView часто не даёт буфер.
  - Тексты ru / en: `music.title` «Музыка из рилса» / «Music from a reel»; `music.placeholder` «Вставьте ссылку на рилс» / «Paste a reel link»; `music.paste` «Вставить» / «Paste»; `music.checking` «Достаём звук…» / «Getting the sound…»; `music.ready` «Трек {t}» / «Track {t}»; `music.hint` «Звук рилса тихо заиграет под вашей речью» / «The reel's sound plays quietly under your voice»; `music.clear` «Убрать музыку» / «Remove music»; `music.dropped` — как в Task 5.

- [ ] **Step 1: Implement markup, state and styles.** Стили на токенах темы, как у `.card.options`. Поле высотой не меньше 44 px, крестик и «Вставить» — мишени по 44 px. Длинная ссылка в поле не раздвигает карточку (`min-width: 0`, `text-overflow: ellipsis`).

- [ ] **Step 2: Headless check on the stand** (рецепт в памяти `miniapp-headless-preview`). Заглушка `api/music` отвечает `{"music_id": "…", "duration": 36.1}`. Снять шаг 2 в состояниях empty / checking / ready / error на ширинах 320 и 390 в обеих темах. Аудит из рецепта: нет `scrollWidth > clientWidth`, мишени ≥ 40 px, текст ≥ 12 px.
Expected: по снимкам строка музыки на месте, ничего не вылезает. Снимки посмотреть глазами.

- [ ] **Step 3: Check the finish body in both layouts.** На стенде перехватить `fetch` и убедиться, что при `ready` в теле `finish` есть `music_id`, при `checking` и `error` его нет. Проверить в «Одно видео» и в «Два видео»: в обоих поле музыки видно на шаге 2, и в «Двух видео» `music_id` уходит вместе с `top_upload_id`. Переключение раскладки трек не сбрасывает.

- [ ] **Step 4: Commit** — только свои ханки трёх файлов.

```bash
git add src/webapp/static/index.html src/webapp/static/app.js src/webapp/static/app.css
git commit -m "Mini App: поле «Музыка из рилса» на шаге оформления"
```

---

### Task 7: Проверка целиком и выкладка

**Files:** без новых. Сервер: `~/reelbot` и `~/reelbot-runtime`.

- [ ] **Step 1: Full test gates**

Run: `pytest -q` и `pytest -m slow -q` (или `tools/check.sh`, если он есть в дереве)
Expected: всё зелёное, кроме тестов, которые падали на `main` и до этой работы. Их сверить с `main` и перечислить.

- [ ] **Step 2: End-to-end locally.** `tests/run_pipeline.py` или `process_video` на фикстурном видео с `music_file`, скачанным из `DfXkZV0Sbuu`. Послушать: речь разборчива, музыка слышна в паузах. Сверить `ebur128`: музыка в паузах примерно на `target − 16`.

- [ ] **Step 3: Final whole-branch review** — свежий агент-ревьюер по всей ветке против описания. Исправить подтверждённое.

- [ ] **Step 4: Merge and push.** Влить worktree в `main` (fast-forward или rebase поверх свежего `main`), `git push`.

- [ ] **Step 5: Deploy.** Проверить, доехал ли CI (`ssh redstar cat reelbot/.deployed`). Если нет — по файлам (рецепт в памяти `tgbot-video-deploy-pushes-to-prod`). Затем:
  - поставить yt-dlp: `~/reelbot-runtime/bin/uv pip install --python ~/reelbot-runtime/venv/bin/python yt-dlp==2026.10.26`;
  - обновить `~/reelbot/pyproject.toml`, иначе `update.sh` при следующей выкладке поставит зависимости заново;
  - убедиться, что в БД нет QUEUED и RUNNING;
  - `systemctl --user restart reelbot-migrate`, затем `reelbot-worker reelbot-janitor reelbot-bot`;
  - вписать в `.deployed`, что привезено;
  - удалить пробную папку `~/ytdlp-probe`.

- [ ] **Step 6: Smoke on prod.** С сервера: `curl` на `127.0.0.1:8099/api/music` нельзя без initData. Вместо этого запустить `fetch_reel_audio` на `DfXkZV0Sbuu` из `~/reelbot` питоном рантайма. Ожидание: трек около 36 с. Записать итог в память и сообщить пользователю, что выложено и как проверить в Mini App.
