"""Собрать библиотеку фоновой музыки: `assets/music/*.m4a` и `catalog.json`.

    python tools/music_fetch.py                 # добрать до 3 треков на настроение
    python tools/music_fetch.py --per-mood 4    # до 4
    python tools/music_fetch.py --listen C:/путь/музыка.html   # страница для прослушивания
    python tools/music_fetch.py --drop calm-02 upbeat-01        # выбросить треки

Откуда треки. Каталог Openverse (api.openverse.org), источник Freesound, лицензия
ТОЛЬКО CC0: такие записи можно вставлять в чужой коммерческий ролик без подписи
автора. CC BY сюда не берётся (см. `src/pipeline/music.py`, «Про лицензии»).
Страница каждого трека пишется в каталог (`source`) — лицензию можно
перепроверить глазами.

Чего скрипт НЕ умеет: слушать. Он отсеивает по тегам (вокал, хоррор, шумы,
эффекты), по длине и по разбросу громкости, приводит громкость к одной и режет
до полутора минут — но хорош ли трек, решает человек. Для этого `--listen`:
страница с плеерами и ключами; неудачные убираются через `--drop`.

Запуск повторяемый: уже лежащие треки не скачиваются заново, настроение
добирается до нужного числа. Ключи в сети не нужны, денег скрипт не тратит.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.core.proc import ffmpeg  # noqa: E402
from src.pipeline.audio import loudnorm_filter, measure_loudness  # noqa: E402
from src.pipeline.audio_post import _audio_duration  # noqa: E402
from src.pipeline.music import CATALOG, LIBRARY_LUFS  # noqa: E402

API = "https://api.openverse.org/v1/audio/"
AGENT = "reelbot-music-library/1.0"

# Настроения библиотеки: (ключ, настроение для модели, энергия, запросы).
# Спокойных больше одного вида намеренно: без модели трек берётся из них.
MOODS: list[tuple[str, str, str, list[str]]] = [
    ("calm", "спокойный, тёплый", "low",
     ["calm piano music", "gentle acoustic guitar music", "soft relaxing music loop"]),
    ("lofi", "расслабленный, лоуфай", "low",
     ["lofi chill beat", "chill hip hop instrumental", "mellow beat loop"]),
    ("serious", "серьёзный, сдержанный", "low",
     ["minimal piano loop", "thoughtful ambient music", "soft cinematic background"]),
    ("upbeat", "бодрый, позитивный", "mid",
     ["upbeat happy music", "happy acoustic music loop", "positive background music"]),
    ("inspiring", "вдохновляющий, светлый", "mid",
     ["inspiring piano music", "uplifting background music", "hopeful music loop"]),
    ("business", "деловой, уверенный", "mid",
     ["corporate background music", "technology background music", "clean electronic music loop"]),
    ("energetic", "энергичный, драйвовый", "high",
     ["energetic electronic music", "funky groove loop", "driving upbeat beat"]),
]

# Что должно быть среди тегов или в названии, чтобы запись считалась музыкой…
MUSICAL = {
    "music", "loop", "instrumental", "background", "beat", "song", "soundtrack", "melody",
    "piano", "guitar", "lofi", "lo-fi", "chill", "groove", "track",
}
# …и с чем её не берём: голос, жуть, шумы, эффекты, сезонное, игровое, сырьё.
BANNED = {
    "vocal", "vocals", "voice", "voices", "singing", "singer", "sing", "speech", "talking",
    "spoken", "rap", "choir", "lyrics", "horror", "creepy", "scary", "spooky", "dark", "eerie",
    "sad", "melancholy", "tension", "suspense", "thriller", "noise", "glitch", "distorted",
    "distortion", "drone", "field-recording", "fieldrecording", "field", "ambience",
    "soundscape", "sfx", "fx", "effect", "effects", "sound-effect", "foley", "christmas", "xmas",
    "halloween", "8bit", "8-bit", "chiptune", "chip-tune", "game-over", "jingle", "stinger",
    "intro", "outro", "ringtone", "alarm", "experimental", "weird", "metal", "dubstep",
    "hardstyle", "trap", "remix", "cover", "test", "demo", "sample-pack", "one-shot", "oneshot",
    "live", "concert", "festival", "radio", "phone", "meditation", "sleep", "baby", "lullaby",
    "music-box", "circus", "comedy", "funny", "cartoon",
    # Добавлено по первому сбору (27.11.2026): волынщики с улицы прошли как
    # «бодрый», тема «по мотивам Трупа невесты» — как «вдохновляющий».
    "bagpipes", "pipes", "pipers", "street", "city", "pixel", "stressful", "inspired",
    "corpse", "funeral", "war", "battle",
}
MIN_SECONDS, MAX_SECONDS = 45.0, 360.0
KEEP_SECONDS = 90.0  # столько от трека остаётся: дальше он зацикливается сведением
MAX_LRA = 9.0  # разброс громкости, LU: трек с тихим вступлением и громким припевом не фон
PER_AUTHOR = 2


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9-]+", text.lower()) if w}


def acceptable(row: dict) -> str | None:
    """Почему запись не годится — или `None`, если годится (по одним метаданным)."""
    if (row.get("license") or "").lower() != "cc0":
        return "лицензия не CC0"
    seconds = (row.get("duration") or 0) / 1000
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        return f"длина {seconds:.0f} с"
    tags = {str(t.get("name", "")).lower() for t in row.get("tags") or []}
    bag = tags | _words(row.get("title") or "")
    bag |= {part for tag in tags for part in tag.split("-")}
    if bag & BANNED:
        return "теги: " + ", ".join(sorted(bag & BANNED)[:3])
    if not bag & MUSICAL:
        return "по тегам это не музыка"
    if not (row.get("url") or "").startswith("https://"):
        return "нет ссылки на звук"
    return None


def search(client: httpx.Client, query: str, page: int = 1) -> list[dict]:
    params = {"q": query, "license": "cc0", "source": "freesound", "page_size": 20, "page": page}
    for attempt in range(3):
        r = client.get(f"{API}?{urlencode(params)}")
        if r.status_code == 429:  # анонимный доступ ограничен по частоте — ждём и повторяем
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json().get("results") or []
    return []


def prepare(raw: Path, dst: Path) -> tuple[float, float] | None:
    """Скачанный файл → трек библиотеки. Возвращает (длина, разброс) или `None`.

    Тишина в начале срезается (первый такт должен звучать сразу), громкость
    приводится к `LIBRARY_LUFS` двумя проходами, трек режется до `KEEP_SECONDS` и
    получает короткие фейды: сведение его зацикливает, и стык без фейда щёлкает.
    """
    if _audio_duration(raw) is None:
        return None
    head = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,"
    measured = measure_loudness(raw, target_lufs=LIBRARY_LUFS, pre=head)
    if not measured or measured.get("input_i", -99) <= -45:
        return None
    lra = float(measured.get("input_lra", 0.0))
    if lra > MAX_LRA:
        return None
    total = _audio_duration(raw) or 0.0
    keep = min(KEEP_SECONDS, total)
    chain = (
        f"{head}{loudnorm_filter(measured, target_lufs=LIBRARY_LUFS)},"
        f"atrim=0:{keep:.2f},afade=t=in:st=0:d=0.3,afade=t=out:st={max(0.0, keep - 1.5):.2f}:d=1.5"
    )
    ffmpeg(["-i", str(raw), "-vn", "-af", chain, "-ar", "48000", "-ac", "2",
            "-c:a", "aac", "-b:a", "96k", str(dst)], timeout=300)
    real = _audio_duration(dst)
    return (real, lra) if real and real >= MIN_SECONDS * 0.6 else None


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return rows.get("tracks", []) if isinstance(rows, dict) else rows


def save(path: Path, rows: list[dict]) -> None:
    rows.sort(key=lambda r: r["key"])
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def fetch(root: Path, per_mood: int) -> None:
    catalog = root / CATALOG
    rows = load(catalog)
    taken = {r.get("source") for r in rows}
    authors: dict[str, int] = {}
    for r in rows:
        authors[r.get("author", "")] = authors.get(r.get("author", ""), 0) + 1
    tmp = root / "_download.tmp"
    with httpx.Client(headers={"User-Agent": AGENT}, timeout=60, follow_redirects=True) as client:
        for mood_key, mood, energy, queries in MOODS:
            have = sorted(r["key"] for r in rows if r["key"].startswith(mood_key + "-"))
            print(f"[{mood_key}] есть {len(have)}, нужно {per_mood}")
            for query in queries:
                if len(have) >= per_mood:
                    break
                for row in search(client, query):
                    if len(have) >= per_mood:
                        break
                    page = row.get("foreign_landing_url") or row.get("url")
                    author = row.get("creator") or ""
                    why = acceptable(row)
                    if page in taken or authors.get(author, 0) >= PER_AUTHOR:
                        continue
                    if why:
                        continue
                    number = next(n for n in range(1, 100) if f"{mood_key}-{n:02d}" not in have)
                    key = f"{mood_key}-{number:02d}"
                    dst = root / f"{key}.m4a"
                    try:
                        tmp.write_bytes(client.get(row["url"]).raise_for_status().content)
                        made = prepare(tmp, dst)
                    except Exception as e:  # noqa: BLE001 — один трек не повод бросать сбор
                        print(f"   - {row.get('title')!r}: не скачан ({e})")
                        made = None
                    finally:
                        tmp.unlink(missing_ok=True)
                    if made is None:
                        dst.unlink(missing_ok=True)
                        print(f"   - {row.get('title')!r}: не прошёл замер (тихий, неровный или битый)")
                        continue
                    tags = [str(t.get("name", "")) for t in row.get("tags") or []][:10]
                    rows.append({
                        "key": key, "file": dst.name, "title": (row.get("title") or key)[:80],
                        "mood": mood, "energy": energy,
                        "about": "теги автора: " + ", ".join(tags),
                        "duration": round(made[0], 1), "license": "cc0", "author": author,
                        "source": page,
                    })
                    have.append(key)
                    taken.add(page)
                    authors[author] = authors.get(author, 0) + 1
                    save(catalog, rows)
                    print(f"   + {key}: {row.get('title')!r}, {made[0]:.0f} с, разброс {made[1]:.1f} LU")
                time.sleep(3)  # анонимный доступ к каталогу: не чаще запроса в несколько секунд
    print(f"в библиотеке {len(rows)} треков → {catalog}")


def drop(root: Path, keys: list[str]) -> None:
    catalog = root / CATALOG
    rows = load(catalog)
    for row in [r for r in rows if r["key"] in keys]:
        (root / row["file"]).unlink(missing_ok=True)
        rows.remove(row)
        print(f"убран {row['key']}: {row['title']!r}")
    save(catalog, rows)


def listen(root: Path, out: Path) -> None:
    """Страница с плеерами: слушать и решать, что оставить. Звук — ссылками на файлы."""
    rows = load(root / CATALOG)
    items = "".join(
        f"<article><h2>{html.escape(r['key'])} <small>{html.escape(r['mood'])}</small></h2>"
        f"<p>{html.escape(r['title'])} — {html.escape(r.get('author', ''))}, "
        f"<a href='{html.escape(r.get('source', ''))}'>страница трека</a>, {r.get('duration', 0):.0f} с</p>"
        f"<audio controls preload='none' src='{(root / r['file']).resolve().as_uri()}'></audio></article>"
        for r in rows
    )
    out.write_text(
        "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Музыка библиотеки</title>"
        "<style>body{font:16px/1.5 system-ui;max-width:720px;margin:24px auto;padding:0 16px}"
        "article{padding:12px 0;border-bottom:1px solid #8884}h2{margin:0;font-size:18px}"
        "small{font-weight:400;opacity:.7}p{margin:4px 0 8px;font-size:14px}audio{width:100%}</style>"
        f"<h1>Фоновая музыка: {len(rows)} треков</h1>"
        "<p>Не понравился трек — запомните его ключ (например, calm-02) и скажите убрать.</p>"
        f"{items}</html>",
        encoding="utf-8",
    )
    print(f"страница для прослушивания: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--per-mood", type=int, default=3)
    ap.add_argument("--listen", type=Path, help="только собрать страницу с плеерами")
    ap.add_argument("--drop", nargs="+", metavar="КЛЮЧ", help="убрать треки из библиотеки")
    args = ap.parse_args()
    root = Path(settings.music_dir)
    root.mkdir(parents=True, exist_ok=True)
    if args.drop:
        drop(root, args.drop)
    elif args.listen:
        listen(root, args.listen)
    else:
        fetch(root, args.per_mood)


if __name__ == "__main__":
    main()
