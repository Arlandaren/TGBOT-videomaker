"""Доска агентов: кто что делает в этом репозитории и что ещё не ушло в origin.

В одной рабочей папке одновременно работают несколько сессий Claude. Git видит
только общий результат — «изменено 14 файлов», — но не чьи они, и коммит,
который не запушен, для соседней сессии ничем не отличается от запушенного.
Доска собирает это в одно место:

  * коммиты, которых нет ни в одной удалённой ветке (по всем локальным веткам);
  * незакоммиченные файлы и кто из агентов их за собой записал;
  * заявки агентов: задача и файлы, которые она трогает;
  * конфликты: один файл в двух заявках, изменённый файл без хозяина.

Заявки лежат в `.git/agents-board/` — в репозиторий они не попадают, а видны
всем сессиям этой папки.

    python tools/board.py                       # показать доску в терминале
    python tools/board.py claim --agent reelsbot-43 --task "..." файл [файл ...]
    python tools/board.py done --agent reelsbot-43
    python tools/board.py html [--watch]        # страница для человека
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Заявка, которую не обновляли дольше, скорее всего осталась от закрытой сессии.
STALE_HOURS = 6.0


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return done.stdout


def board_dir() -> Path:
    path = ROOT / git("rev-parse", "--git-common-dir").strip() / "agents-board"
    path.mkdir(parents=True, exist_ok=True)
    return path


def norm(path: str) -> str:
    """Путь от корня репозитория, с прямыми слэшами — как его печатает git."""
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(ROOT)
        except ValueError:
            pass
    return p.as_posix().removeprefix("./")


def ago(ts: float) -> str:
    minutes = max(0, int((time.time() - ts) / 60))
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 60 * 24:
        return f"{minutes // 60} ч {minutes % 60} мин назад"
    return f"{minutes // (60 * 24)} дн назад"


# ------------------------------------------------------------------ сбор данных


def claims() -> list[dict]:
    out = []
    for file in sorted(board_dir().glob("*.json")):
        try:
            out.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda c: c.get("updated", 0), reverse=True)


def unpushed() -> list[dict]:
    """Коммиты локальных веток, которых нет ни в одной удалённой ветке."""
    out = []
    branches = git("for-each-ref", "refs/heads", "--format=%(refname:short)").split()
    for branch in branches:
        log = git("log", "--format=%h%x09%an%x09%ct%x09%s", branch, "--not", "--remotes")
        commits = []
        for line in log.splitlines():
            sha, author, ts, subject = line.split("\t", 3)
            files = git("show", "--name-only", "--format=", sha).split("\n")
            commits.append({
                "sha": sha, "author": author, "ts": float(ts), "subject": subject,
                "files": [f for f in files if f],
            })
        if commits:
            out.append({"branch": branch, "commits": commits})
    return out


def dirty() -> list[dict]:
    """Незакоммиченные файлы: изменённые, новые, удалённые."""
    out = []
    entries = git("status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # у переименования вторым идёт старое имя
            i += 1
        kind = "новый" if code == "??" else "удалён" if "D" in code else "изменён"
        full = ROOT / path
        out.append({
            "path": path, "kind": kind,
            "mtime": full.stat().st_mtime if full.exists() else None,
        })
    return out


def snapshot() -> dict:
    all_claims = claims()
    owners: dict[str, list[str]] = {}
    for claim in all_claims:
        for path in claim.get("files", []):
            owners.setdefault(path, []).append(claim["agent"])
    files = dirty()
    for f in files:
        f["owners"] = owners.get(f["path"], [])
    for claim in all_claims:
        claim["stale"] = time.time() - claim.get("updated", 0) > STALE_HOURS * 3600
    return {
        "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "claims": all_claims,
        "unpushed": unpushed(),
        "dirty": files,
        "stashes": [s for s in git("stash", "list").splitlines() if s],
        "shared": {p: who for p, who in owners.items() if len(who) > 1},
        "orphans": [f["path"] for f in files if not f["owners"]],
    }


# ------------------------------------------------------------------ терминал


def render_text(s: dict) -> str:
    lines = [f"ДОСКА АГЕНТОВ · ветка {s['branch']} · {datetime.now():%H:%M:%S}", ""]

    lines.append("КТО ЧТО ДЕЛАЕТ")
    if not s["claims"]:
        lines.append("  заявок нет")
    for c in s["claims"]:
        mark = "  (давно не обновлялась)" if c["stale"] else ""
        lines.append(f"  {c['agent']}: {c['task']} — {ago(c['updated'])}{mark}")
        for path in c.get("files", []):
            lines.append(f"      {path}")

    lines += ["", "КОНФЛИКТЫ"]
    if not s["shared"] and not s["orphans"]:
        lines.append("  нет")
    for path, who in s["shared"].items():
        lines.append(f"  ! {path} — в заявках сразу у: {', '.join(who)}")
    for path in s["orphans"]:
        lines.append(f"  ? {path} — изменён, но ни в одной заявке")

    lines += ["", "НЕ ЗАПУШЕНО (есть только на этой машине)"]
    if not s["unpushed"]:
        lines.append("  все коммиты есть в origin")
    for b in s["unpushed"]:
        lines.append(f"  ветка {b['branch']}: {len(b['commits'])}")
        for c in b["commits"]:
            lines.append(f"    {c['sha']} {c['subject']} — {c['author']}, {ago(c['ts'])}")
            for path in c["files"]:
                lines.append(f"        {path}")
    for stash in s["stashes"]:
        lines.append(f"  отложено (stash): {stash}")

    lines += ["", f"НЕ ЗАКОММИЧЕНО: {len(s['dirty'])}"]
    for f in s["dirty"]:
        who = ", ".join(f["owners"]) or "ничей"
        when = ago(f["mtime"]) if f["mtime"] else ""
        lines.append(f"  {f['kind']:8} {f['path']}  [{who}] {when}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ страница

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__REFRESH__
<title>Доска агентов</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1c1b19; --mute:#6f6b63; --line:#e3e0d9;
        --warn:#b4530a; --warn-bg:#fdf1e3; --ok:#2f6b3c; --tag:#eeece6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161513; --card:#1f1e1b; --ink:#ecebe6; --mute:#9a968c; --line:#34322d;
          --warn:#f0a35a; --warn-bg:#35281a; --ok:#7fc08e; --tag:#2b2a26; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--ink);
       font:15px/1.5 system-ui, "Segoe UI", sans-serif; }
main { max-width:1100px; margin:0 auto; }
header { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:baseline; margin-bottom:20px; }
h1 { font-size:22px; margin:0; }
h2 { font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:var(--mute);
     margin:28px 0 10px; font-weight:600; }
.mute { color:var(--mute); }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.card h3 { margin:0 0 2px; font-size:16px; }
.task { margin:0 0 8px; }
ul { margin:0; padding:0; list-style:none; }
li { padding:2px 0; overflow-wrap:anywhere; }
code { font:13px/1.4 ui-monospace, Consolas, monospace; }
.tag { display:inline-block; padding:1px 8px; border-radius:999px; background:var(--tag);
       font-size:12px; margin-left:6px; white-space:nowrap; }
.tag.warn { background:var(--warn-bg); color:var(--warn); }
.alert { background:var(--warn-bg); color:var(--warn); border-radius:10px; padding:10px 14px;
         margin-bottom:8px; overflow-wrap:anywhere; }
.fine { color:var(--ok); }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:10px; }
td, th { text-align:left; padding:7px 12px; border-top:1px solid var(--line); vertical-align:top; }
th { border-top:0; font-size:12px; color:var(--mute); font-weight:600; }
td.num { white-space:nowrap; color:var(--mute); }
details summary { cursor:pointer; }
</style></head><body><main>
__BODY__
</main></body></html>
"""


def render_html(s: dict, refresh: int = 0) -> str:
    e = html.escape
    out = [
        "<header><h1>Доска агентов</h1>"
        f"<span class='mute'>ветка <code>{e(s['branch'])}</code> · обновлено "
        f"{datetime.now():%H:%M:%S}</span></header>"
    ]

    out.append("<h2>Конфликты</h2>")
    if not s["shared"] and not s["orphans"]:
        out.append("<p class='fine'>Нет: у каждого изменённого файла один хозяин.</p>")
    for path, who in s["shared"].items():
        out.append(
            f"<div class='alert'><code>{e(path)}</code> — в заявках сразу у: "
            f"{e(', '.join(who))}</div>"
        )
    if s["orphans"]:
        items = "".join(f"<li><code>{e(p)}</code></li>" for p in s["orphans"])
        out.append(
            f"<div class='alert'>Изменены, но ни в одной заявке ({len(s['orphans'])}):"
            f"<ul>{items}</ul></div>"
        )

    out.append("<h2>Кто что делает</h2>")
    if not s["claims"]:
        out.append("<p class='mute'>Заявок нет.</p>")
    else:
        out.append("<div class='grid'>")
        for c in s["claims"]:
            stale = "<span class='tag warn'>давно не обновлялась</span>" if c["stale"] else ""
            files = "".join(f"<li><code>{e(p)}</code></li>" for p in c.get("files", []))
            out.append(
                f"<div class='card'><h3>{e(c['agent'])}{stale}</h3>"
                f"<p class='task'>{e(c['task'])}</p>"
                f"<p class='mute'>{ago(c['updated'])}</p><ul>{files}</ul></div>"
            )
        out.append("</div>")

    out.append("<h2>Не запушено — есть только на этой машине</h2>")
    if not s["unpushed"] and not s["stashes"]:
        out.append("<p class='fine'>Все коммиты есть в origin.</p>")
    for b in s["unpushed"]:
        rows = ""
        for c in b["commits"]:
            files = "".join(f"<li><code>{e(p)}</code></li>" for p in c["files"])
            rows += (
                f"<tr><td class='num'><code>{e(c['sha'])}</code></td>"
                f"<td><details><summary>{e(c['subject'])} "
                f"<span class='mute'>· файлов: {len(c['files'])}</span></summary>"
                f"<ul>{files}</ul></details></td>"
                f"<td class='num'>{e(c['author'])}</td><td class='num'>{ago(c['ts'])}</td></tr>"
            )
        out.append(
            f"<p>Ветка <code>{e(b['branch'])}</code><span class='tag warn'>"
            f"{len(b['commits'])} не в origin</span></p>"
            f"<div class='scroll'><table><tr><th>Коммит</th><th>Что</th><th>Автор</th>"
            f"<th>Когда</th></tr>{rows}</table></div>"
        )
    for stash in s["stashes"]:
        out.append(f"<div class='alert'>Отложено (stash): <code>{e(stash)}</code></div>")

    out.append(f"<h2>Не закоммичено — {len(s['dirty'])}</h2>")
    if s["dirty"]:
        rows = ""
        for f in s["dirty"]:
            who = e(", ".join(f["owners"])) if f["owners"] else "<span class='tag warn'>ничей</span>"
            when = ago(f["mtime"]) if f["mtime"] else ""
            rows += (
                f"<tr><td><code>{e(f['path'])}</code></td><td class='num'>{f['kind']}</td>"
                f"<td>{who}</td><td class='num'>{when}</td></tr>"
            )
        out.append(
            "<div class='scroll'><table><tr><th>Файл</th><th>Что</th><th>Чей</th>"
            f"<th>Правка</th></tr>{rows}</table></div>"
        )
    else:
        out.append("<p class='fine'>Рабочая папка чистая.</p>")

    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return PAGE.replace("__REFRESH__", meta).replace("__BODY__", "\n".join(out))


# ------------------------------------------------------------------ команды


def claim_file(agent: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in agent)
    return board_dir() / f"{safe}.json"


def cmd_claim(args: argparse.Namespace) -> int:
    file = claim_file(args.agent)
    old = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
    files = [norm(f) for f in args.files]
    if args.add:
        files = list(dict.fromkeys([*old.get("files", []), *files]))
    record = {
        "agent": args.agent,
        "task": args.task or old.get("task") or "(задача не указана)",
        "files": files,
        "started": old.get("started", time.time()),
        "updated": time.time(),
    }
    file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    taken = {
        path: [c["agent"] for c in claims() if c["agent"] != args.agent and path in c.get("files", [])]
        for path in files
    }
    clash = {p: who for p, who in taken.items() if who}
    print(f"Заявка записана: {args.agent} — {record['task']} ({len(files)} файлов)")
    for path, who in clash.items():
        print(f"  ! {path} уже в заявке у: {', '.join(who)} — договоритесь, прежде чем править")
    return 1 if clash else 0


def cmd_done(args: argparse.Namespace) -> int:
    file = claim_file(args.agent)
    if file.exists():
        file.unlink()
        print(f"Заявка снята: {args.agent}")
    else:
        print(f"Заявки у {args.agent} не было")
    return 0


def cmd_html(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else board_dir() / "board.html"
    while True:
        out.write_text(render_html(snapshot(), args.every if args.watch else 0), encoding="utf-8")
        if not args.watch:
            print(out)
            return 0
        time.sleep(args.every)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Доска агентов этого репозитория")
    sub = parser.add_subparsers(dest="cmd")

    claim = sub.add_parser("claim", help="записать за собой задачу и файлы")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--task", default="")
    claim.add_argument("--add", action="store_true", help="добавить файлы к заявке, а не заменить")
    claim.add_argument("files", nargs="*")
    claim.set_defaults(run=cmd_claim)

    done = sub.add_parser("done", help="снять свою заявку")
    done.add_argument("--agent", required=True)
    done.set_defaults(run=cmd_done)

    page = sub.add_parser("html", help="собрать страницу")
    page.add_argument("--watch", action="store_true", help="пересобирать, пока не остановят")
    page.add_argument("--every", type=int, default=5)
    page.add_argument("--out", default="")
    page.set_defaults(run=cmd_html)

    args = parser.parse_args()
    if not args.cmd:
        print(render_text(snapshot()), end="")
        return 0
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
