"""Нижняя клавиатура: пять кнопок под полем ввода.

Кнопки шлют обычный текст, поэтому роутер подключается ДО `fallback` — тот
ловит всё подряд и принял бы «Начать монтаж» за кнопку прежней версии бота.

Подписи узнаются на всех языках сразу: человек мог нажать кнопку, а потом
сменить язык, и старая клавиатура осталась бы у него в чате ещё надолго —
Telegram хранит её, пока бот не пришлёт другую.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.handlers.payments import share_text
from src.bot.keyboards import (
    app_keyboard, balance_keyboard, main_keyboard, share_keyboard, support_keyboard,
)
from src.core.config import settings
from src.core.i18n import (
    LANGS, lang_of, people_word, plural, t, tokens_text, videos_word,
)
from src.core.referral import code_of
from src.db import repo

log = logging.getLogger(__name__)
router = Router(name="menu")

ACTIONS = ("start", "balance", "videos", "referral", "support", "channel")


def _norm(text: str | None) -> str:
    """Подпись как есть, без регистра и краевых пробелов.

    Сравнение ТОЧНОЕ, вместе с эмодзи. Сначала здесь стояло сравнение по одним
    словам, и «💎 Баланс» прежней клавиатуры совпал с нынешним «💳 Баланс»:
    кнопку забирал этот роутер, а старая клавиатура так и оставалась висеть у
    пользователя, потому что снимает её только `fallback`. Значок — часть
    подписи, по нему кнопки и различаются.
    """
    return (text or "").strip().lower()


# Подписи клавиатуры до значков (ноябрь 2026): эмодзи стояло в самом тексте.
# Такая клавиатура остаётся у людей, пока бот не пришлёт новую, и её кнопки
# обязаны работать как раньше.
PREVIOUS_LABELS: dict[str, dict[str, str]] = {
    "start": {"ru": "▶️ Начать монтаж", "en": "▶️ Start editing"},
    "balance": {"ru": "💳 Баланс", "en": "💳 Balance"},
    "videos": {"ru": "📹 Мои ролики", "en": "📹 My reels"},
    "referral": {"ru": "🎁 Реф. программа", "en": "🎁 Referrals"},
    "support": {"ru": "🎧 Поддержка", "en": "🎧 Support"},
}

# Подпись на любом языке → действие. Считается один раз на импорте.
LABELS: dict[str, str] = {
    **{_norm(label): action for action, by_lang in PREVIOUS_LABELS.items()
       for label in by_lang.values()},
    **{_norm(t(f"kb.{action}", lang)): action for lang in LANGS for action in ACTIONS},
}


def action_for(text: str | None) -> str | None:
    return LABELS.get(_norm(text))


async def _support_contact() -> str | None:
    """Кому писать: настройка, иначе @username первого владельца из базы.

    Поиск по базе — не фокус, а способ обойтись без ещё одной настройки:
    владелец бота и так заходил в него, значит его username уже лежит в
    `users`. Нет ни того, ни другого — честно говорим, что контакт не настроен,
    вместо выдуманного адреса.
    """
    if settings.support_contact.strip():
        return settings.support_contact.strip()
    for admin_id in settings.admin_ids:
        user = await repo.get_user(admin_id)
        if user and user.username:
            return f"@{user.username}"
    log.warning("контакт поддержки не задан: SUPPORT_CONTACT пуст и у владельца нет @username")
    return None


async def _db_user(user):
    return await repo.upsert_user(
        user.id, username=user.username, first_name=user.first_name,
        language_code=user.language_code, is_admin=user.id in settings.admin_ids,
    )


@router.message(F.text.func(lambda text: action_for(text) is not None))
async def on_button(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    db_user = await _db_user(user)
    action = action_for(message.text)
    log.info("кнопка %r от %s", action, user.id)
    await run_action(action, message, db_user, bot)


# Инлайн-кнопки под сообщениями меню: `menu:<действие>`.
INLINE_ACTIONS = ("start", "referral", "faq", "operator", "partner", "ref_terms")


@router.callback_query(lambda c: bool(c.data) and c.data.startswith("menu:"))
async def on_inline(callback: CallbackQuery, bot: Bot) -> None:
    action = (callback.data or "").split(":", 1)[1]
    if (
        callback.from_user is None or action not in INLINE_ACTIONS
        or not isinstance(callback.message, Message)
    ):
        await callback.answer()
        return
    db_user = await _db_user(callback.from_user)
    await callback.answer()
    log.info("инлайн-кнопка %r от %s", action, callback.from_user.id)
    await run_action(action, callback.message, db_user, bot, user=callback.from_user)


async def run_action(action: str | None, message: Message, db_user, bot: Bot, user=None) -> None:
    """Ответ на действие меню.

    `message` — куда отвечать. У инлайн-кнопки это сообщение БОТА, поэтому
    человека берём из `user`, а не из `message.from_user`.
    """
    user = user or message.from_user
    lang = lang_of(db_user)

    if action == "start":
        # Инлайн-кнопка несёт СВЕЖИЙ адрес приложения — в отличие от клавиатуры,
        # которая лежит в чате с прошлого запуска.
        await message.answer(
            t(
                "start.steps", lang,
                min_s=f"{settings.min_video_seconds:.0f}",
                max_m=f"{settings.max_video_minutes:.0f}",
            ),
            reply_markup=app_keyboard(None, lang, "btn.begin", "start"),
        )
    elif action == "balance":
        done = db_user.videos_done or 0
        enough = db_user.balance // max(1, settings.tokens_per_video)
        await message.answer(
            t(
                "menu.balance", lang,
                balance=tokens_text(db_user.balance, lang),
                enough=enough,
                done=done,
            ),
            reply_markup=balance_keyboard(lang),
        )
    elif action == "videos":
        await message.answer(t("menu.videos", lang), reply_markup=app_keyboard("videos", lang))
    elif action == "referral":
        # Ссылка несёт код приглашения (`src/core/referral.py`): без него бонус
        # пригласившему не привязать — `/start` разбирает именно этот параметр.
        if settings.referral_percent <= 0:
            await message.answer(t("menu.referral_off", lang))
            return
        me = await bot.me()
        link = f"https://t.me/{me.username}?start={code_of(user.id)}"
        # Счётчики — справка, а не суть сообщения: без базы ссылка всё равно
        # должна прийти, пусть и с нулями.
        invited = earned = 0
        try:
            stats = await repo.referral_stats(user.id, since=datetime.now(timezone.utc))
            invited, earned = stats["invited"], stats["earned"]
        except Exception as e:  # noqa: BLE001
            log.warning("сводка приглашений для %s не собралась: %s", user.id, e)
        # Превью ссылки не выключаем: под текстом встаёт карточка бота, как у
        # Brandly, — видно, куда ведёт ссылка, которой делятся.
        await message.answer(
            t(
                "menu.referral", lang,
                percent=settings.referral_percent,
                link=link,
                invited=f"{invited} {people_word(invited, lang)}",
                earned=share_text(earned, lang),
            ),
            reply_markup=share_keyboard(link, lang),
        )
    elif action == "channel":
        # Ссылку на канал владелец пришлёт позже: до тех пор кнопка честно
        # говорит, что канал скоро появится, а не ведёт в никуда.
        url = settings.channel_url.strip()
        if url:
            kb = InlineKeyboardBuilder()
            kb.button(text=t("btn.open_channel", lang), url=url)
            await message.answer(t("menu.channel", lang), reply_markup=kb.as_markup())
        else:
            await message.answer(t("menu.channel_soon", lang))
    elif action == "support":
        await message.answer(t("menu.support", lang), reply_markup=support_keyboard(lang))
    elif action == "faq":
        await message.answer(faq_text(lang))
    elif action == "operator":
        contact = await _support_contact()
        await message.answer(
            t("menu.operator", lang, contact=contact) if contact else t("menu.support_unset", lang)
        )
    elif action == "partner":
        contact = await _support_contact()
        await message.answer(
            t("menu.partner", lang, contact=contact) if contact else t("menu.partner_unset", lang)
        )
    elif action == "ref_terms":
        await message.answer(t("menu.ref_terms", lang, percent=settings.referral_percent))


def days_word(n: int, lang: str | None) -> str:
    return plural(n, lang, ("день", "дня", "дней"), ("day", "days"))


def faq_text(lang: str) -> str:
    """FAQ из настроек: цена, проба, лимиты, хранение — те же числа, по которым
    работает бот, а не переписанные в текст и забытые при следующей смене цены."""
    from src.bot.handlers.common import offer_pack  # круговой импорт: common тянет menu

    # Режим `free_videos` здесь не объявляется: «монтаж бесплатный» владелец
    # запретил писать где бы то ни было (16.11.2026, `test_i18n`).
    cost = tokens_text(settings.tokens_per_video, lang)
    trial = max(0, settings.trial_videos)
    if trial == 0:
        trial_line = ""
    elif trial == 1:
        trial_line = t("faq.trial_one", lang)
    else:
        trial_line = t("faq.trial_many", lang, trial=f"{trial} {videos_word(trial, lang)}")
    pack = offer_pack(lang)
    price = t(
        "faq.cost_paid", lang, cost=cost, trial=trial_line,
        pack=t("faq.pack", lang, pack=pack) if pack else "",
    )
    days = settings.storage_retention_days
    storage = (
        t("faq.storage", lang, days=f"{days} {days_word(days, lang)}") if days > 0
        else t("faq.storage_forever", lang)
    )
    return "\n\n".join([
        t("faq.title", lang),
        price,
        t("faq.limits", lang, min_s=f"{settings.min_video_seconds:.0f}",
          max_m=f"{settings.max_video_minutes:.0f}"),
        t("faq.tokens", lang, cost=cost),
        storage,
        t("faq.more", lang),
    ])


async def show_keyboard(message: Message, lang: str) -> None:
    """Прислать саму клавиатуру. Отдельным сообщением: к тексту с инлайн-кнопками
    её не прицепить — разметка у сообщения одна."""
    await message.answer(t("kb.ready", lang), reply_markup=main_keyboard(lang))


# Отметка в `users.settings`: клавиатуру этому человеку уже присылали.
# «kb4» — клавиатура 2 × 2 как у Brandly (19.11.2026). У кого отметка «kb»,
# «kb2» или «kb3» (прежние клавиатуры), тот получит новую один раз на следующем
# сообщении.
SEEN_FLAG = "kb4"


class KeyboardOnce(BaseMiddleware):
    """Один раз довезти клавиатуру тем, у кого её ещё нет.

    Прежняя версия бота свою клавиатуру снимала, а новую Telegram сам не
    покажет: реплай-клавиатура приходит только с сообщением. Без этого новые
    кнопки увидели бы лишь те, кто сам наберёт /start, — то есть почти никто.

    Отметка живёт в `users.settings`, поэтому переживает перезапуск и приходит
    ровно один раз. Любой сбой здесь гасится: клавиатура — удобство, а не
    повод не ответить на сообщение.
    """

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None or not isinstance(event, Message):
            return result
        try:
            db_user = await repo.get_user(user.id)
            if db_user is None or (db_user.settings or {}).get(SEEN_FLAG):
                return result
            await show_keyboard(event, lang_of(db_user))
            await repo.set_user_settings(user.id, **{SEEN_FLAG: 1})
        except Exception as e:  # noqa: BLE001
            log.warning("клавиатуру не довезли пользователю %s: %s", user.id, e)
        return result
