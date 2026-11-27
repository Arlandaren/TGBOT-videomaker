"""Редактор сцен: что из монтажного листа показать человеку и как применить его правки.

Сцена редактора — блок субтитров листа (`CaptionBlock`): его текст, слова,
место и размер текста, картинка поверх. Человек правит сцены в приложении,
сюда приходит словарь правок, отсюда выходит НОВЫЙ лист — версия ролика
собирается из него тем же путём, что перерендер, без распознавания и без LLM.

Здесь только данные: ни базы, ни файлов, ни времени. Поэтому всё проверяется
тестами без стенда (`tests/test_edit.py`).

Что правка может и чего не может.

* Текст сцены меняет ТЕКСТ слов, а не их число и не их время: слово в листе —
  это ещё и отрезок звука, и резы, вставки, звуки и графика ссылаются на слова
  по номерам. Новых слов больше, чем было, — хвост уезжает в последнее слово;
  меньше — лишние слова в кадре просто не показываются (звучать продолжают).
* «Вырезать слова» убирает слова из ролика совсем: из кадра и из звука.
* Правила режиссёра (не больше двух картинок, одно выделенное слово) писались
  для модели. Человеку они не указ, кроме тех, без которых кадр ломается.
"""

from __future__ import annotations

from typing import Any

from src.core.edl import (
    BLOCK_MAX_WORDS,
    BLOCK_POSTER_MAX_WORDS,
    EDL,
    LIST_MIN_ITEMS,
    Broll,
    BrollAnim,
    CaptionBlock,
    Cut,
    Timeline,
    list_runs,
)

POSITIONS = ("auto", "top", "center", "bottom")
SIZES = ("s", "m", "l")
# Длина текста одной сцены и заголовка ролика. Сцена — до восьми слов; предел
# стоит от случайно вставленного абзаца, а не от длинной фразы.
MAX_SCENE_TEXT = 160
MAX_TITLE = 80
# Пауза после вырезанного слова уходит вместе с ним, если она короче этого:
# иначе на месте слова остаётся двойная тишина — до него и после.
SWALLOW_GAP = 0.5
# Кусок короче этого после вырезания слова не оставляем: это не кадр, а вспышка.
MIN_PIECE = 0.08
# Своя картинка стоит в кадре столько, сколько звучит сцена, в пределах обычной
# карточки (`pipeline.broll.FULLSCREEN_SECONDS`).
MEDIA_MIN, MEDIA_MAX = 1.5, 2.5
# Между своими картинками — зазор: две карточки разом рендерер не разводит.
MEDIA_GAP = 0.3


class EditError(ValueError):
    """Правку нельзя применить. `code` — ключ фразы для человека (`api.edit_*`)."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code)


def _timeline(edl: EDL, apply_cuts: bool) -> Timeline:
    return edl.timeline() if apply_cuts else Timeline([], edl.source_duration)


def _movable(block: CaptionBlock) -> bool:
    """Можно ли переставить текст сцены. Пункт списка стоит в своей плашке, а
    длинный тихий блок бывает только наверху: кеглем плаката он не влезет."""
    if block.item:
        return False
    return not (block.quiet_register() and len(block.words) > BLOCK_POSTER_MAX_WORDS)


def scenes_of(edl: EDL, *, apply_cuts: bool) -> list[dict[str, Any]]:
    """Сцены ролика для редактора, по порядку. Времена — на таймлайне результата.

    `i` — номер блока в листе: им сцена называется в правках. Блок, все слова
    которого вырезаны нарезкой, сценой не становится — в ролике его нет.
    """
    tl = _timeline(edl, apply_cuts)
    by_idx = {w.idx: w for w in edl.words}
    out: list[dict[str, Any]] = []
    for bi, block in enumerate(edl.captions):
        shown = []
        for cw in block.words:
            word = by_idx.get(cw.word_idx)
            window = tl.word_window(word) if word is not None else None
            if word is not None and window is not None:
                shown.append((cw, word, window))
        if not shown:
            continue
        ids = {cw.word_idx for cw, _, _ in shown}
        media = next((b for b in edl.broll if b.word_idx in ids), None)
        out.append(
            {
                "i": bi,
                "start": round(shown[0][2][0], 3),
                "end": round(shown[-1][2][1], 3),
                "text": " ".join(w.text for _, w, _ in shown),
                "words": [
                    {"idx": w.idx, "text": w.text, "em": cw.emphasis.value != "none"}
                    for cw, w, _ in shown
                ],
                "position": block.position,
                "size": block.size,
                "movable": _movable(block),
                "media": (
                    {
                        # Слово, на котором картинка появляется, — подпись «На картинке».
                        "word": by_idx[media.word_idx].text,
                        "custom": bool(media.asset_key),
                    }
                    if media is not None
                    else None
                ),
            }
        )
    return out


def _tokens(text: Any, limit: int) -> list[str]:
    if not isinstance(text, str):
        raise EditError("edit_bad", "текст сцены — строка")
    text = " ".join(text.split())
    if len(text) > limit:
        raise EditError("edit_text_long", f"текст длиннее {limit} знаков")
    return text.split(" ") if text else []


def _spread(count: int, slots: int) -> list[int]:
    """Какие `count` из `slots` мест занять, чтобы текст шёл через всю сцену:
    первое и последнее — всегда, остальные равномерно между ними."""
    if count >= slots:
        return list(range(slots))
    if count == 1:
        return [0]
    return sorted({round(i * (slots - 1) / (count - 1)) for i in range(count)})


def _carve(cuts: list[Cut], holes: list[tuple[float, float]]) -> list[Cut]:
    """Вырезать отрезки из оставленных кусков: кусок делится на «до» и «после»."""
    out: list[Cut] = []
    for cut in cuts:
        if not cut.kept:
            out.append(cut)
            continue
        pieces = [(cut.src_in, cut.src_out)]
        for a, b in holes:
            fresh: list[tuple[float, float]] = []
            for p_in, p_out in pieces:
                if b <= p_in or a >= p_out:
                    fresh.append((p_in, p_out))
                    continue
                if a - p_in >= MIN_PIECE:
                    fresh.append((p_in, a))
                if p_out - b >= MIN_PIECE:
                    fresh.append((b, p_out))
            pieces = fresh
        for n, (p_in, p_out) in enumerate(pieces):
            # Первый кусок остаётся тем, чем был (зацепка остаётся зацепкой),
            # продолжения — обычные оставленные куски той же крупности.
            reason = cut.reason if n == 0 and abs(p_in - cut.src_in) < 1e-6 else "keep"
            out.append(Cut(src_in=round(p_in, 3), src_out=round(p_out, 3), reason=reason, framing=cut.framing))
    return out


def apply_edits(edl: EDL, edits: dict[str, Any], *, apply_cuts: bool) -> tuple[EDL, bool]:
    """Правки редактора → новый лист. Исходный лист не меняется.

    `edits`: `{"scenes": {"<i>": {"text"?, "cut"?, "position"?, "size"?,
    "media"?}}, "title"?}`; `media` — `{"op": "remove"}` или
    `{"op": "set", "key": "<ключ файла>"}`.

    Возвращает лист и новое значение «нарезка применяется»: вырезанное слово
    включает нарезку и у ролика, собранного без неё, — тогда режется ТОЛЬКО
    это слово, а паузы остаются, как человек и просил.
    """
    if not isinstance(edits, dict):
        raise EditError("edit_bad", "правки — словарь")
    scenes = edits.get("scenes", {})
    if scenes is None:
        scenes = {}
    if not isinstance(scenes, dict):
        raise EditError("edit_bad", "scenes — словарь")

    new = edl.model_copy(deep=True)
    by_idx = {w.idx: w for w in new.words}
    tl = _timeline(new, apply_cuts)
    dropped: set[int] = set()
    custom: list[Broll] = []

    for raw_i, change in scenes.items():
        try:
            bi = int(raw_i)
        except (TypeError, ValueError):
            raise EditError("edit_bad", f"сцена {raw_i!r}") from None
        if not (0 <= bi < len(new.captions)) or not isinstance(change, dict):
            raise EditError("edit_bad", f"сцена {raw_i!r}")
        block = new.captions[bi]
        ids = [cw.word_idx for cw in block.words]

        cut = change.get("cut") or []
        if not isinstance(cut, list) or not set(cut) <= set(ids):
            raise EditError("edit_bad", f"сцена {bi}: вырезать можно только её слова")
        dropped |= set(cut)
        left = [cw for cw in block.words if cw.word_idx not in dropped]
        # Слова, которых нет в ролике (попали под нарезку), текстом не правятся.
        live = [cw for cw in left if cw.word_idx in by_idx and tl.word_window(by_idx[cw.word_idx])]

        if change.get("text") is not None:
            tokens = _tokens(change["text"], MAX_SCENE_TEXT)
            if not tokens:
                left = []
            elif live:
                take = _spread(len(tokens), len(live))
                slots = [live[k] for k in take]
                texts = tokens[: len(slots) - 1] + [" ".join(tokens[len(slots) - 1 :])]
                for cw, text in zip(slots, texts):
                    by_idx[cw.word_idx].text = text
                keep = {cw.word_idx for cw in slots}
                left = [cw for cw in left if cw.word_idx in keep]
        block.words = left

        place = change.get("position")
        if place is not None:
            if place not in POSITIONS:
                raise EditError("edit_bad", f"сцена {bi}: место {place!r}")
            if _movable(block) or place == block.position:
                block.position = place
                if place != "auto":
                    # Явное место отменяет привязку к голове: иначе блок всё
                    # равно ездил бы за лицом, а не стоял, где его поставили.
                    block.anchor_track = None
        size = change.get("size")
        if size is not None:
            if size not in SIZES:
                raise EditError("edit_bad", f"сцена {bi}: размер {size!r}")
            block.size = size

        media = change.get("media")
        if media is not None:
            if not isinstance(media, dict) or media.get("op") not in ("remove", "set"):
                raise EditError("edit_bad", f"сцена {bi}: картинка")
            new.broll = [b for b in new.broll if b.word_idx not in ids]
            if media["op"] == "set":
                key = media.get("key")
                first = next((cw for cw in live if cw.word_idx not in dropped), None)
                if not isinstance(key, str) or not key or first is None:
                    raise EditError("edit_bad", f"сцена {bi}: картинке не к чему привязаться")
                words = [by_idx[cw.word_idx] for cw in live if cw.word_idx not in dropped]
                spoken = words[-1].end - words[0].start
                custom.append(
                    Broll(
                        word_idx=first.word_idx,
                        duration=round(min(MEDIA_MAX, max(MEDIA_MIN, spoken)), 2),
                        query="photo",
                        anim=BrollAnim.POP_IN,
                        anchor="card",
                        asset_key=key,
                    )
                )

    if "title" in edits and edits["title"] is not None:
        title = " ".join(_tokens(edits["title"], MAX_TITLE))
        new.title = title or None

    # Блоки приводятся к тому, что рендерер умеет нарисовать.
    for block in new.captions:
        if block.quiet_register() and block.position != "top":
            if len(block.words) > BLOCK_POSTER_MAX_WORDS:
                block.position = "top"
            else:
                block.quiet = False
    new.captions = [b for b in new.captions if b.words]
    for run in list_runs(new.captions):
        if len(run) < LIST_MIN_ITEMS:
            # Список из одного пункта списком не бывает — пункт играет плакатом.
            for bi in run:
                new.captions[bi].item = False
                new.captions[bi].emoji = None
    if any(len(b.words) > BLOCK_MAX_WORDS for b in new.captions):
        raise EditError("edit_bad", "в сцене слишком много слов")

    cut_on = apply_cuts
    if dropped:
        if not apply_cuts or not new.kept_cuts:
            # Ролик собран без нарезки (или резов в листе нет вовсе): режем
            # только то, что вырезал человек, — из цельного исходника.
            new.cuts = [Cut(src_in=0.0, src_out=new.source_duration, reason="keep")]
            cut_on = True
        holes: list[tuple[float, float]] = []
        for idx in sorted(dropped):
            word = by_idx[idx]
            nxt = by_idx.get(idx + 1)
            end = word.end
            if nxt is not None and idx + 1 not in dropped and 0 <= nxt.start - word.end < SWALLOW_GAP:
                end = nxt.start
            if holes and (idx - 1 in dropped or word.start - holes[-1][1] < 1e-3):
                # Соседние вырезанные слова — одна дыра: пауза между ними
                # осталась бы обрывком в десятую секунды.
                holes[-1] = (holes[-1][0], max(holes[-1][1], end))
            else:
                holes.append((word.start, end))
        new.cuts = _carve(new.cuts, holes)
        if not new.kept_cuts:
            raise EditError("edit_empty", "вырезано всё — в ролике ничего не осталось")
        new.sfx = [s for s in new.sfx if s.at_word_idx not in dropped]

    # Картинки, слово которых вырезано, уходят вместе с ним; свои встают в лист
    # и не наезжают друг на друга и на прежние.
    out_tl = _timeline(new, cut_on)
    new.broll = [b for b in new.broll if b.word_idx not in dropped]
    for item in custom:
        if item.word_idx not in dropped:
            new.broll.append(item)
    new.broll.sort(key=lambda b: b.word_idx)
    busy_until = -1.0
    for item in new.broll:
        window = out_tl.word_window(by_idx[item.word_idx]) if item.word_idx in by_idx else None
        if window is None:
            continue
        if window[0] < busy_until + MEDIA_GAP:
            raise EditError("edit_media_close", "картинки стоят слишком близко друг к другу")
        busy_until = window[0] + item.duration

    return new, cut_on


def changed(before: EDL, after: EDL) -> bool:
    """Отличается ли лист после правок: «Собрать» без изменений ничего не стоит."""
    return before.model_dump() != after.model_dump()
