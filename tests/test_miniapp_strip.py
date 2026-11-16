"""Лента стилей в Mini App не ездит сама.

Человек листает ленту и целится в плитку. Лента, которая в этот момент едет
сама, уводит плитку из-под пальца — и это случалось дважды, по разным причинам:

1. После выбора лента ставила выбранное по центру (убрано раньше, `keepScroll`).
2. Шрифты плиток приезжают не сразу — по туннелю секундами. Каждый приехавший
   шрифт менял ширину своей плитки и сдвигал всё, что правее, а когда приезжал
   последний, лента ещё и прокручивалась к выбранному стилю. На стенде с
   мгновенными шрифтами этого не видно; с задержкой 2.5 с плитка уезжала на
   49 px, а затем лента прыгала ещё на 89.

Поведение проверяется стендом с медленными шрифтами (см. память проекта), а
здесь закреплено то, без чего оно возвращается.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "src" / "webapp" / "static" / "app.js").read_text()


def body_of(name: str) -> str:
    start = APP.index(f"function {name}(")
    end = APP.index("\nfunction ", start + 1)
    return APP[start:end]


def test_the_strip_is_put_to_the_chosen_style_only_when_the_step_opens():
    calls = [m.start() for m in re.finditer(r"(?<!function )scrollToActive\(", APP)]
    assert len(calls) == 1, "лента ставится к выбранному один раз — при открытии шага"
    assert "scrollToActive" in body_of("setStep")


def test_fonts_arriving_late_never_scroll_the_strip():
    for handler in re.findall(r"document\.fonts\.ready\.then\((.*?)\)\s*[;)]", APP, re.S):
        assert "scrollToActive" not in handler
    # Обработчик открытия шага после шрифтов пересчитывает стрелки и превью — и всё.
    refit = APP[APP.index("const refit = () => {"):]
    refit = refit[: refit.index("};")]
    assert "syncAllArrows" in refit and "preview.render" in refit
    assert "scroll" not in refit.replace("syncAllArrows", "")


def test_a_pill_changing_width_does_not_move_the_tapped_pill():
    """Якорь ленты: поправка прокрутки на каждое изменение ширины плиток."""
    assert "new ResizeObserver(() => holdAnchor(box))" in body_of("watchPills")
    hold = body_of("holdAnchor")
    assert "anchor.x" in hold and "jumpTo(box, box.scrollLeft + drift)" in hold
    # После перерисовки ленты якорь — выбранная плитка, и следим уже за новыми.
    keep = body_of("keepScroll")
    assert "pinAnchor(" in keep and "watchPills(box)" in keep and "is-active" in keep
    # Свою поправку прокрутка не перезаписывает: иначе копится округление.
    assert "heldAt === box.scrollLeft" in body_of("saveAnchor")


def test_a_jump_is_never_turned_into_a_ride():
    """У ленты плавная прокрутка в CSS; мгновенная постановка обязана её выключать."""
    jump = body_of("jumpTo")
    assert 'box.style.scrollBehavior = "auto"' in jump
    assert jump.index('scrollBehavior = "auto"') < jump.index("box.scrollLeft = left")
    assert "jumpTo(box, left)" in body_of("keepScroll")
