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
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.handlers.payments import share_text
from src.bot.keyboards import app_keyboard, main_keyboard, share_keyboard
from src.core.config import settings
from src.core.i18n import LANGS, lang_of, people_word, t, tokens_text, videos_word
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


@router.message(F.text.func(lambda text: action_for(text) is not None))
async def on_button(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    db_user = await repo.upsert_user(
        user.id, username=user.username, first_name=user.first_name,
        language_code=user.language_code, is_admin=user.id in settings.admin_ids,
    )
    lang = lang_of(db_user)
    action = action_for(message.text)
    log.info("кнопка %r от %s", action, user.id)

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
                enough=f"{enough} {videos_word(enough, lang)}",
                done=f"{done} {videos_word(done, lang)}",
            ),
            reply_markup=app_keyboard("balance", lang, "btn.topup", "balance"),
        )
    elif action == "videos":
        await message.answer(t("menu.videos", lang), reply_markup=app_keyboard("videos", lang))
    elif action == "referral":
        # Ссылка несёт код приглашения (`src/core/referral.py`): без него бонус
        # пригласившему не привязать — `/start` разбирает именно этот параметр.
        if settings.referral_percent <= 0:
            await message.answer(t("menu.referral_off", lang))
        else:
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
            await message.answer(
                t(
                    "menu.referral", lang,
                    percent=settings.referral_percent,
                    link=link,
                    invited=f"{invited} {people_word(invited, lang)}",
                    earned=share_text(earned, lang),
                ),
                reply_markup=share_keyboard(link, lang),
                disable_web_page_preview=True,
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
    else:
        contact = await _support_contact()
        await message.answer(
            t("menu.support", lang, contact=contact) if contact else t("menu.support_unset", lang)
        )


async def show_keyboard(message: Message, lang: str) -> None:
    """Прислать саму клавиатуру. Отдельным сообщением: к тексту с инлайн-кнопками
    её не прицепить — разметка у сообщения одна."""
    await message.answer(t("kb.ready", lang), reply_markup=main_keyboard(lang))


# Отметка в `users.settings`: клавиатуру этому человеку уже присылали.
# «kb3» — клавиатура со значками и кнопкой «Наш канал». У кого отметка «kb» или
# «kb2» (прежние клавиатуры), тот получит новую один раз на следующем сообщении.
SEEN_FLAG = "kb3"


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
