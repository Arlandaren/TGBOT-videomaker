"""Нижняя клавиатура бота: раскладка, узнавание подписей, адрес приложения."""

from __future__ import annotations

import pytest

from src.bot.handlers import fallback, menu
from src.bot.keyboards import main_keyboard, set_webapp_url
from src.core.i18n import LANGS, t

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _tunnel_url():
    """Адрес приложения живёт в модуле — возвращаем его после каждого теста."""
    from src.bot import keyboards

    was = keyboards.webapp_url()
    set_webapp_url("https://example.trycloudflare.com")
    yield
    set_webapp_url(was)


def test_the_keyboard_is_three_rows_like_the_reference():
    """Раскладка как у Brandly (27.11.2026): «Начать монтаж» во всю ширину,
    «Баланс · Мои ролики», «Реф. программа · Поддержка»."""
    kb = main_keyboard("ru")
    assert [len(row) for row in kb.keyboard] == [1, 2, 2]
    assert [b.text for row in kb.keyboard for b in row] == [
        "Начать монтаж", "Баланс", "Мои ролики", "Реф. программа", "Поддержка",
    ]
    # Без этих двух клавиатура либо занимает пол-экрана, либо исчезает после
    # первого нажатия — и пользователь остаётся без навигации.
    assert kb.resize_keyboard and kb.is_persistent


def test_every_label_is_recognised_in_every_language():
    """Клавиатура остаётся в чате после смены языка: Telegram хранит её, пока
    бот не пришлёт другую. Значит подписи надо узнавать на всех языках сразу."""
    for lang in LANGS:
        for action in menu.ACTIONS:
            assert menu.action_for(t(f"kb.{action}", lang)) == action, (lang, action)
    assert menu.action_for("привет") is None
    assert menu.action_for(None) is None


def test_labels_do_not_collide_between_languages():
    """Одна подпись — одно действие. Совпадение означало бы, что кнопка делает
    разное в зависимости от языка, на котором её завели."""
    seen: dict[str, tuple[str, str]] = {}
    for lang in LANGS:
        for action in menu.ACTIONS:
            key = menu._norm(t(f"kb.{action}", lang))
            assert key not in seen or seen[key][1] == action, (key, seen.get(key), lang, action)
            seen[key] = (lang, action)


def test_the_tunnel_address_is_not_baked_into_the_button(monkeypatch):
    """С туннелем адрес новый на каждом запуске, а клавиатуру Telegram хранит у
    пользователя. Вшитый туда адрес назавтра ведёт в никуда, и кнопка молча
    перестаёт открывать приложение — поэтому при туннеле она обычная."""
    from src.bot import keyboards

    monkeypatch.setattr(keyboards.settings, "webapp_url", "", raising=False)
    assert main_keyboard("ru").keyboard[0][0].web_app is None

    monkeypatch.setattr(keyboards.settings, "webapp_url", "https://reels.example", raising=False)
    button = main_keyboard("ru").keyboard[0][0]
    assert button.web_app is not None
    assert button.web_app.url == "https://example.trycloudflare.com", (
        "адрес берётся тот, что поднят сейчас, а не переписанный из настройки"
    )


def test_the_old_keyboard_handler_keeps_its_hands_off_the_new_buttons():
    """«Начать монтаж» есть и на старой клавиатуре, и на новой. Если бы его
    забирал fallback, ответом было бы «кнопки обновились» вместе со снятием
    самой клавиатуры — и так на каждое нажатие."""
    for action in menu.ACTIONS:
        for lang in LANGS:
            assert fallback.legacy_action(t(f"kb.{action}", lang)) is None
    # Настоящие старые кнопки он при этом по-прежнему узнаёт.
    assert fallback.legacy_action("📊 Моя статистика") == "stats"
    assert fallback.legacy_action("💎 Баланс") == "balance"


# ------------------------------------------------- доставка клавиатуры в старые чаты


def _message(user_id: int = 5):
    from datetime import datetime, timezone

    from aiogram.types import Chat, Message, User

    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Т"),
    )


class _User:
    def __init__(self, settings):
        self.settings = settings
        self.lang = "ru"
        self.language_code = "ru"


async def test_the_keyboard_reaches_an_old_chat_exactly_once(monkeypatch):
    """Прежняя версия бота клавиатуру сняла, а новую Telegram сам не покажет:
    реплай-клавиатура приходит только с сообщением. Без доставки новые кнопки
    увидели бы лишь те, кто сам наберёт /start."""
    from src.db import repo

    shown: list[int] = []
    saved: list[dict] = []
    stored = {"settings": {}}

    async def get_user(_uid):
        return _User(stored["settings"])

    async def set_settings(_uid, **values):
        saved.append(values)
        stored["settings"] = {**stored["settings"], **values}

    async def show(message, lang):
        shown.append(message.chat.id)

    monkeypatch.setattr(repo, "get_user", get_user)
    monkeypatch.setattr(repo, "set_user_settings", set_settings)
    monkeypatch.setattr(menu, "show_keyboard", show)

    async def handler(_event, _data):
        return "ответ"

    once = menu.KeyboardOnce()
    assert await once(handler, _message(), {}) == "ответ"
    assert shown == [5] and saved == [{menu.SEEN_FLAG: 1}]

    # Второе сообщение — уже без клавиатуры: отметка легла в `users.settings`.
    assert await once(handler, _message(), {}) == "ответ"
    assert shown == [5], "клавиатура приходит один раз, а не на каждое сообщение"


async def test_a_broken_delivery_never_eats_the_answer(monkeypatch):
    """Клавиатура — удобство. Сбой базы не должен отменять ответ на сообщение."""
    from src.db import repo

    async def boom(_uid):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(repo, "get_user", boom)

    async def handler(_event, _data):
        return "ответ"

    assert await menu.KeyboardOnce()(handler, _message(), {}) == "ответ"


# ------------------------------------------------------------ «Наш канал»


def test_buttons_gone_from_the_keyboard_still_work_on_old_keyboards():
    """«Наш канал» с клавиатуры ушёл, но у людей висит прежняя. «Мои ролики»
    27.11.2026 вернулись на клавиатуру."""
    labels = {b.text for row in main_keyboard("ru").keyboard for b in row}
    assert t("kb.videos", "ru") in labels
    assert t("kb.channel", "ru") not in labels
    for action in ("videos", "channel"):
        assert menu.action_for(t(f"kb.{action}", "ru")) == action


def test_the_channel_keyboard_reaches_old_chats_once_more():
    """Клавиатуру в три ряда получат и те, у кого уже была прежняя."""
    assert menu.SEEN_FLAG not in ("kb", "kb2", "kb3", "kb4")


async def test_the_channel_button_opens_the_channel(monkeypatch):
    from types import SimpleNamespace

    from src.db import repo

    sent = []

    class Msg:
        from_user = SimpleNamespace(id=5, username=None, first_name="Т", language_code="ru")
        text = "Наш канал"

        async def answer(self, text, **k):
            sent.append((text, k.get("reply_markup")))

    async def upsert(*a, **k):
        return SimpleNamespace(id=5, lang="ru", language_code="ru", interface_lang=None, balance=0)

    monkeypatch.setattr(repo, "upsert_user", upsert)
    monkeypatch.setattr(menu.settings, "channel_url", "https://t.me/cliply_channel")
    await menu.on_button(Msg(), bot=None)
    text, markup = sent[-1]
    assert markup.inline_keyboard[0][0].url == "https://t.me/cliply_channel"

    monkeypatch.setattr(menu.settings, "channel_url", "")
    await menu.on_button(Msg(), bot=None)
    text, markup = sent[-1]
    assert markup is None and text == t("menu.channel_soon", "ru")


# ------------------------------------------------ /start без строки-носителя


class _Chat:
    id = 5


class _StartMsg:
    """Сообщение-двойник: собирает всё, что бот отправил в ответ."""

    def __init__(self):
        from types import SimpleNamespace

        self.from_user = SimpleNamespace(id=5, username=None, first_name="Т", language_code="ru")
        self.chat = _Chat()
        self.sent: list[tuple[str, object]] = []
        self.deleted = False

    async def answer(self, text, **k):
        self.sent.append((text, k.get("reply_markup")))

    async def delete(self):
        self.deleted = True


def _patch_db(monkeypatch, lang="ru"):
    from types import SimpleNamespace

    from src.bot.handlers import common
    from src.db import repo

    saved: list[dict] = []

    async def upsert(*a, **k):
        return SimpleNamespace(id=5, lang=lang, language_code="ru", interface_lang=lang, balance=0)

    async def set_settings(_uid, **values):
        saved.append(values)

    async def set_lang(*a, **k):
        return None

    monkeypatch.setattr(common, "_upsert", upsert)
    monkeypatch.setattr(repo, "set_user_settings", set_settings)
    monkeypatch.setattr(repo, "set_user_lang", set_lang)
    return saved


async def test_start_sends_the_welcome_then_the_menu(monkeypatch):
    """Как у Brandly: приветствие с инлайн-кнопками, следом «Меню внизу 👇»
    с нижней клавиатурой (просьба владельца 19.11.2026)."""
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

    from src.bot.handlers import common

    saved = _patch_db(monkeypatch)
    msg = _StartMsg()
    await common.on_start(msg, None)
    assert [type(m) for _, m in msg.sent] == [InlineKeyboardMarkup, ReplyKeyboardMarkup]
    assert msg.sent[0][0] == common.welcome_text("ru")
    assert msg.sent[1][0] == "Меню внизу 👇"
    assert {menu.SEEN_FLAG: 1} in saved, "иначе разовая доставка пришлёт клавиатуру ещё раз"


async def test_picking_a_language_greets_with_the_menu_too(monkeypatch):
    from types import SimpleNamespace

    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

    from src.bot.handlers import common

    saved = _patch_db(monkeypatch)
    msg = _StartMsg()
    monkeypatch.setattr(common, "Message", _StartMsg)

    async def apply_menu(*a, **k):
        return None

    monkeypatch.setattr(common, "apply_menu_button", apply_menu)

    async def answer(*a, **k):
        return None

    callback = SimpleNamespace(data="lang:ru", from_user=msg.from_user, message=msg, answer=answer)
    await common.on_language_pick(callback, bot=None)
    assert msg.deleted, "сообщение с выбором языка убирается, а не висит рядом с приветствием"
    assert [type(m) for _, m in msg.sent] == [InlineKeyboardMarkup, ReplyKeyboardMarkup]
    assert msg.sent[0][0] == common.welcome_text("ru")
    assert {menu.SEEN_FLAG: 1} in saved
