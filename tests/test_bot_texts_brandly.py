"""Экраны бота слово в слово как у Brandly (скриншоты владельца, 19.11.2026).

Тексты сверены с образцом построчно; цвета кнопок — `style` Bot API:
`success` — зелёная, `primary` — акцентная (в фиолетовой теме — сиреневая),
без стиля — цвет темы.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.bot import icons, keyboards
from src.bot.handlers import common, menu
from src.core.i18n import t

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(icons, "_enabled", True)
    was = keyboards.webapp_url()
    keyboards.set_webapp_url("https://example.trycloudflare.com")
    monkeypatch.setattr(keyboards.settings, "channel_url", "https://t.me/brand_channel")
    monkeypatch.setattr(menu.settings, "brand_name", "Brandly")
    yield
    keyboards.set_webapp_url(was)


def _rows(markup):
    return [[(b.text, b.style) for b in row] for row in markup.inline_keyboard]


def test_welcome_text_matches_the_reference():
    assert common.welcome_text("ru") == (
        "Привет! Я — бот-монтажёр Brandly 🎬\n\n"
        "Кидаете мне сырое видео, где вы говорите на камеру, а я сам:\n\n"
        "✅ вырежу все паузы и «эээ»\n"
        "✅ сделаю картинку динамичной\n"
        "✅ поставлю трендовые субтитры\n"
        "✅ добавлю картинки по смыслу\n"
        "✅ можно править в редакторе\n\n"
        "На выходе — готовый Reels. Без монтажёра, без CapCut."
    )


def test_welcome_buttons_green_start_and_channel():
    kb = keyboards.welcome_inline_keyboard("ru")
    assert _rows(kb) == [[("Начать монтаж", "success")], [("Официальный канал", None)]]
    assert kb.inline_keyboard[0][0].callback_data == "menu:start"
    assert kb.inline_keyboard[1][0].url == "https://t.me/brand_channel"


def test_no_channel_button_without_a_channel(monkeypatch):
    monkeypatch.setattr(keyboards.settings, "channel_url", "")
    assert _rows(keyboards.welcome_inline_keyboard("ru")) == [[("Начать монтаж", "success")]]


def test_bottom_menu_colours():
    kb = keyboards.main_keyboard("ru")
    assert [[(b.text, b.style) for b in row] for row in kb.keyboard] == [
        [("Начать монтаж", "primary"), ("Баланс", "success")],
        [("Реф. программа", None), ("Поддержка", None)],
    ]
    assert t("kb.ready", "ru") == "Меню внизу 👇"


def test_balance_screen():
    # Вместе с огрехами образца: «~0 монтажа», «1 роликов» — так попросил владелец.
    assert t("menu.balance", "ru", balance="0 токенов", enough=0, done=1) == (
        "Ваш баланс: 0 токенов\nХватит на ~0 монтажа\nСмонтировано всего: 1 роликов"
    )
    kb = keyboards.balance_keyboard("ru")
    assert _rows(kb) == [[("Пополнить баланс", "success")], [("Пригласить друга", None)]]
    assert kb.inline_keyboard[0][0].web_app is not None
    assert kb.inline_keyboard[1][0].callback_data == "menu:referral"


def test_referral_screen():
    text = t("menu.referral", "ru", percent=10, link="L", invited="0 человек", earned="0 токенов")
    assert text.endswith(
        "Приглашено: 0 человек\nЗаработано: 0 токенов\n\n"
        "Самозанятым и ИП — деньгами, а не токенами: нажмите «Стать партнёром»."
    )
    assert _rows(keyboards.share_keyboard("https://t.me/x?start=1", "ru")) == [
        [("Поделиться", "primary")], [("💼 Стать партнёром", None)], [("Условия программы", None)],
    ]


def test_support_screen():
    assert t("menu.support", "ru") == (
        "Чем помочь?\n\n"
        "В FAQ — короткие ответы на частые вопросы: сколько стоит ролик и где смотреть цены, "
        "лимиты видео, как считаются токены и сколько хранятся готовые ролики.\n\n"
        "Если видео не проходит, монтаж завис, списались лишние токены или вопрос не из FAQ — "
        "пишите оператору, разберёмся."
    )
    kb = keyboards.support_keyboard("ru")
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [
        ["FAQ"], ["Оператор"], ["🇬🇧 English"],
    ]
    assert kb.inline_keyboard[2][0].callback_data == "lang:en"
    assert keyboards.support_keyboard("en").inline_keyboard[2][0].callback_data == "lang:ru"


def test_faq_takes_its_numbers_from_the_settings(monkeypatch):
    s = menu.settings
    monkeypatch.setattr(s, "free_videos", False)
    monkeypatch.setattr(s, "trial_videos", 1)
    monkeypatch.setattr(s, "tokens_per_video", 23)
    monkeypatch.setattr(s, "storage_retention_days", 7)
    text = menu.faq_text("ru")
    assert "Один монтаж — 23 токена. Первый ролик — бесплатно." in text
    assert "7 дней" in text
    assert f"до {s.max_video_minutes:.0f} мин" in text

    monkeypatch.setattr(s, "free_videos", True)
    assert "бесплатный" not in menu.faq_text("ru"), "запрет владельца от 16.11.2026"


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **k):
        self.sent.append((text, k.get("reply_markup")))


@pytest.mark.parametrize("action", menu.INLINE_ACTIONS)
async def test_every_inline_button_answers(monkeypatch, action):
    """Каждая инлайн-кнопка меню что-то отвечает, а не крутит часики."""
    from aiogram.types import Message

    from src.db import repo

    db_user = SimpleNamespace(id=5, lang="ru", language_code="ru", interface_lang="ru",
                              balance=0, videos_done=0, username=None)

    async def upsert(*a, **k):
        return db_user

    async def stats(*a, **k):
        return {"invited": 0, "earned": 0}

    async def get_user(_uid):
        return None

    monkeypatch.setattr(repo, "upsert_user", upsert)
    monkeypatch.setattr(repo, "referral_stats", stats)
    monkeypatch.setattr(repo, "get_user", get_user)
    monkeypatch.setattr(menu, "Message", _Msg)

    class _Bot:
        async def me(self):
            return SimpleNamespace(username="brandly_app")

    msg = _Msg()
    answered = []

    async def cb_answer(*a, **k):
        answered.append(True)

    callback = SimpleNamespace(
        data=f"menu:{action}", message=msg, answer=cb_answer,
        from_user=SimpleNamespace(id=5, username=None, first_name="Т", language_code="ru"),
    )
    assert Message  # настоящий тип подменён двойником только внутри menu
    await menu.on_inline(callback, bot=_Bot())
    assert answered and msg.sent, action
