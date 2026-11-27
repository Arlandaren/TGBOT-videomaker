"""Язык интерфейса: русский и английский. Все тексты, которые видит пользователь, — здесь.

Переводится только ИНТЕРФЕЙС: сообщения бота, приложение, стадии и ошибки
монтажа. Язык речи в роликах от этого выбора не зависит (`WHISPER_LANGUAGE`).

Пайплайн пользовательских фраз не пишет: он поднимает `JobError` с кодом, а
фразу по коду и языку собирает тот, кто показывает её человеку. Иначе русский
текст ошибки, записанный в Redis воркером, англоязычный пользователь увидел бы
как есть.
"""

from __future__ import annotations

from typing import Any

LANGS = ("ru", "en")
DEFAULT_LANG = "ru"

# Кому по умолчанию русский, пока язык не выбран руками. Telegram отдаёт язык
# клиента; из СНГ-клиентов русский интерфейс понятнее английского.
_RU_CLIENTS = {"ru", "uk", "be", "kk", "uz", "ky", "tg", "hy", "az"}

LANG_NAMES = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English"}


def guess_lang(language_code: str | None) -> str:
    code = (language_code or "").lower().split("-")[0]
    if not code:
        return DEFAULT_LANG
    return "ru" if code in _RU_CLIENTS else "en"


def lang_of(user: Any) -> str:
    """Выбранный язык пользователя, а если не выбран — угаданный по клиенту."""
    if user is None:
        return DEFAULT_LANG
    chosen = getattr(user, "lang", None)
    if chosen in LANGS:
        return chosen
    return guess_lang(getattr(user, "language_code", None))


def normalize(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def t(key: str, lang: str | None = None, **params: Any) -> str:
    """Текст по ключу. Нет перевода — русский; нет ключа — сам ключ, а не падение."""
    entry = TEXTS.get(key)
    if entry is None:
        return key
    text = entry.get(normalize(lang)) or entry[DEFAULT_LANG]
    return text.format(**params) if params else text


def plural(n: int, lang: str | None, forms_ru: tuple[str, str, str], forms_en: tuple[str, str]) -> str:
    if normalize(lang) == "en":
        return forms_en[0] if abs(n) == 1 else forms_en[1]
    a = abs(n) % 100
    if 11 <= a <= 14:
        return forms_ru[2]
    return {1: forms_ru[0], 2: forms_ru[1], 3: forms_ru[1], 4: forms_ru[1]}.get(a % 10, forms_ru[2])


def people_word(n: int, lang: str | None) -> str:
    return plural(n, lang, ("человек", "человека", "человек"), ("person", "people"))


def videos_word(n: int, lang: str | None) -> str:
    return plural(n, lang, ("ролик", "ролика", "роликов"), ("video", "videos"))


def tokens_word(n: int, lang: str | None) -> str:
    return plural(n, lang, ("токен", "токена", "токенов"), ("token", "tokens"))


def tokens_text(n: int, lang: str | None) -> str:
    """«240 токенов», «1 token»."""
    return f"{n} {tokens_word(n, lang)}"


# Коды ошибок монтажа. Пайплайн знает только код, фразу подставляет интерфейс.
ERROR_CODES = (
    "source_lost", "edl_missing", "too_short", "too_long", "no_audio",
    "no_speech", "edl_invalid", "cut_broken", "crop_broken", "worker_lost", "failed",
    "cover_failed",
)

STAGE_KEYS = ("probe", "asr", "clean", "direct", "broll", "cut", "audio", "crop", "render", "done")


TEXTS: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------ язык
    "lang.pick": {
        "ru": "Выберите язык интерфейса · Choose your language",
        "en": "Choose your language · Выберите язык интерфейса",
    },
    "lang.set": {"ru": "Язык интерфейса: русский", "en": "Interface language: English"},

    # ------------------------------------------------------------ старт и помощь
    "start.welcome": {
        "ru": (
            "Привет! Я — бот-монтажёр {brand} 🎬\n\n"
            "Кидаете мне сырое видео, где вы говорите на камеру, а я сам:\n\n"
            "✅ вырежу все паузы и «эээ»\n"
            "✅ сделаю картинку динамичной\n"
            "✅ поставлю трендовые субтитры\n"
            "✅ добавлю картинки по смыслу\n"
            "✅ можно править в редакторе\n\n"
            "На выходе — готовый Reels. Без монтажёра, без CapCut."
        ),
        "en": (
            "Hi! I'm {brand}, your video editing bot 🎬\n\n"
            "Send me a raw video of you talking on camera, and I'll do the rest:\n\n"
            "✅ cut every pause and “umm”\n"
            "✅ make the picture dynamic\n"
            "✅ add trending captions\n"
            "✅ add images that match the meaning\n"
            "✅ you can fine-tune it in the editor\n\n"
            "You get a finished Reel. No editor, no CapCut."
        ),
    },
    # Кнопки под приветствием (как у Brandly): зелёная «Начать монтаж» и канал.
    "btn.channel": {"ru": "Официальный канал", "en": "Official channel"},
    # Ответ на «Начать монтаж»: что делать дальше, тремя шагами.
    "start.steps": {
        "ru": (
            "✨ Всего три шага:\n\n"
            "1. Загружаете видео, где вы говорите на камеру\n"
            "2. Выбираете стиль и оформление\n"
            "3. Через пару минут забираете готовый Reels\n\n"
            "Видео от {min_s} с до {max_m} мин, подойдёт и горизонтальное."
        ),
        "en": (
            "✨ Just three steps:\n\n"
            "1. Upload a video of you talking on camera\n"
            "2. Pick a style and look\n"
            "3. A few minutes later, collect your finished Reel\n\n"
            "Videos from {min_s} s to {max_m} min, landscape works too."
        ),
    },
    "btn.begin": {"ru": "Приступить", "en": "Get started"},
    "btn.topup": {"ru": "Пополнить баланс", "en": "Top up"},
    "btn.invite": {"ru": "Пригласить друга", "en": "Invite a friend"},
    # Условия одной строкой под приветствием (`handlers.common.offer_text`).
    # `cost` — цена ролика в токенах, `pack` — самый маленький пакет.
    "start.offer_trial_one": {
        "ru": "🎁 Первый ролик — бесплатно. Дальше ролик — {cost}, пакеты от {pack}.",
        "en": "🎁 Your first reel is free. After that a reel is {cost}, packs from {pack}.",
    },
    "start.offer_trial_many": {
        "ru": "🎁 Первые {trial} — бесплатно. Дальше ролик — {cost}, пакеты от {pack}.",
        "en": "🎁 Your first {trial} are free. After that a reel is {cost}, packs from {pack}.",
    },
    "start.offer_paid": {
        "ru": "💳 Ролик — {cost}, пакеты от {pack}.",
        "en": "💳 A reel is {cost}, packs from {pack}.",
    },
    # «240 токенов за 1099 ₽ (749 ⭐)»: рубли — ориентир, платят звёздами.
    "start.pack": {"ru": "{tokens} за {rub} ₽ ({stars} ⭐)", "en": "{tokens} for {rub} ₽ ({stars} ⭐)"},
    "start.pack_stars": {"ru": "{tokens} за {stars} ⭐", "en": "{tokens} for {stars} ⭐"},
    "btn.open_app": {"ru": "Открыть приложение", "en": "Open the app"},
    "btn.menu": {"ru": "Приложение ✨", "en": "App ✨"},
    # Нижняя клавиатура. Эмодзи — часть подписи: по ним кнопку узнают быстрее,
    # чем по словам, а на десктопе без цветного шрифта они рисуются контуром.
    # Без эмодзи: значок кнопки — контурный кастомный эмодзи (`bot/icons.py`).
    # Прежние подписи с эмодзи узнаёт `menu.PREVIOUS_LABELS`.
    "kb.start": {"ru": "Начать монтаж", "en": "Start editing"},
    "kb.balance": {"ru": "Баланс", "en": "Balance"},
    "kb.videos": {"ru": "Мои ролики", "en": "My reels"},
    "kb.referral": {"ru": "Реф. программа", "en": "Referrals"},
    "kb.support": {"ru": "Поддержка", "en": "Support"},
    "kb.channel": {"ru": "Наш канал", "en": "Our channel"},
    "menu.channel": {
        "ru": "Новости, примеры роликов и советы по съёмке — в нашем канале.",
        "en": "News, example reels and filming tips — in our channel.",
    },
    "menu.channel_soon": {"ru": "Наш канал скоро появится — следите за новостями.", "en": "Our channel is coming soon."},
    "btn.open_channel": {"ru": "Открыть канал", "en": "Open the channel"},
    "kb.ready": {"ru": "Меню внизу 👇", "en": "Menu below 👇"},
    "menu.videos": {
        "ru": "Ваши ролики — откройте приложение, чтобы посмотреть детали и прогресс монтажа:",
        "en": "Your reels — open the app to see the details and editing progress:",
    },
    # Слово в слово как у Brandly (просьба владельца 19.11.2026: «прям 1в1»),
    # вместе с их неизменяемыми «монтажа» и «роликов» — «1 роликов» тоже так.
    "menu.balance": {
        "ru": "Ваш баланс: {balance}\nХватит на ~{enough} монтажа\nСмонтировано всего: {done} роликов",
        "en": "Your balance: {balance}\nEnough for ~{enough} edits\nEdited in total: {done} reels",
    },
    "menu.referral": {
        "ru": (
            "Приглашайте друзей — получайте токены.\n"
            "За каждую успешную оплату приглашённого друга получаете {percent}% "
            "от купленных им токенов на баланс.\n\n"
            "Ваша ссылка:\n{link}\n\n"
            "Приглашено: {invited}\n"
            "Заработано: {earned}\n\n"
            "Самозанятым и ИП — деньгами, а не токенами: нажмите «Стать партнёром»."
        ),
        "en": (
            "Invite friends — earn tokens.\n"
            "For every successful payment by a friend you invited, you get {percent}% "
            "of the tokens they bought on your balance.\n\n"
            "Your link:\n{link}\n\n"
            "Invited: {invited}\n"
            "Earned: {earned}\n\n"
            "Self-employed or a company? Get paid in money, not tokens: tap “Become a partner”."
        ),
    },
    "btn.partner": {"ru": "💼 Стать партнёром", "en": "💼 Become a partner"},
    "btn.ref_terms": {"ru": "Условия программы", "en": "Programme terms"},
    # «Стать партнёром»: выплаты деньгами договариваются с владельцем лично —
    # готового договора в боте нет, поэтому ведём к живому человеку.
    "menu.partner": {
        "ru": (
            "Партнёрам — самозанятым и ИП — платим за приглашённых деньгами, а не токенами.\n\n"
            "Напишите {contact}: расскажем условия и оформим партнёрство."
        ),
        "en": (
            "Partners who are self-employed or run a company get paid in money, not tokens.\n\n"
            "Write to {contact}: we'll explain the terms and set up the partnership."
        ),
    },
    "menu.partner_unset": {
        "ru": "Партнёрская программа для самозанятых и ИП скоро откроется — следите за новостями.",
        "en": "The partner programme for the self-employed and companies is opening soon.",
    },
    # Правила — те, что проверяет `repo.attach_referrer` и начисление бонуса.
    "menu.ref_terms": {
        "ru": (
            "Условия программы приглашений\n\n"
            "1. Друг открывает бота по вашей ссылке.\n"
            "2. С каждой его успешной оплаты вам на баланс приходит {percent}% "
            "купленных им токенов.\n"
            "3. Друг засчитывается, только если перешёл по ссылке до своей первой покупки "
            "и ещё не приглашён кем-то другим.\n"
            "4. Пригласить самого себя нельзя.\n\n"
            "Бонусные токены тратятся так же, как купленные."
        ),
        "en": (
            "Referral programme terms\n\n"
            "1. Your friend opens the bot through your link.\n"
            "2. From each of their successful payments you get {percent}% "
            "of the tokens they bought on your balance.\n"
            "3. A friend counts only if they followed the link before their first purchase "
            "and weren't invited by someone else.\n"
            "4. You can't invite yourself.\n\n"
            "Bonus tokens are spent just like bought ones."
        ),
    },
    "menu.referral_off": {
        "ru": "Программа приглашений сейчас выключена.",
        "en": "The referral programme is switched off right now.",
    },
    "menu.referral_share": {"ru": "Поделиться", "en": "Share"},
    "menu.support": {
        "ru": (
            "Чем помочь?\n\n"
            "В FAQ — короткие ответы на частые вопросы: сколько стоит ролик и где смотреть "
            "цены, лимиты видео, как считаются токены и сколько хранятся готовые ролики.\n\n"
            "Если видео не проходит, монтаж завис, списались лишние токены или вопрос "
            "не из FAQ — пишите оператору, разберёмся."
        ),
        "en": (
            "How can we help?\n\n"
            "The FAQ has short answers to common questions: what a reel costs and where to see "
            "prices, video limits, how tokens are counted and how long finished reels are kept.\n\n"
            "If a video won't go through, an edit is stuck, extra tokens were charged or your "
            "question isn't in the FAQ — write to an operator and we'll sort it out."
        ),
    },
    "btn.faq": {"ru": "FAQ", "en": "FAQ"},
    "btn.operator": {"ru": "Оператор", "en": "Operator"},
    # Кнопка смены языка под «Поддержкой» — подпись на ДРУГОМ языке.
    "btn.switch_lang": {"ru": "🇬🇧 English", "en": "🇷🇺 Русский"},
    "menu.operator": {
        "ru": "Напишите {contact} — опишите, что случилось, и мы разберёмся.",
        "en": "Write to {contact} — tell us what happened and we'll sort it out.",
    },
    # FAQ собирается из кусков (`menu.faq_text`): цена и сроки берутся из настроек.
    "faq.title": {"ru": "Частые вопросы", "en": "FAQ"},
    "faq.cost_paid": {
        "ru": "💰 Сколько стоит ролик?\nОдин монтаж — {cost}.{trial} Пакеты токенов и цены — "
              "в приложении, раздел «Баланс»{pack}.",
        "en": "💰 What does a reel cost?\nOne edit is {cost}.{trial} Token packs and prices are "
              "in the app, “Balance” section{pack}.",
    },
    "faq.trial_one": {"ru": " Первый ролик — бесплатно.", "en": " Your first reel is free."},
    "faq.trial_many": {"ru": " Первые {trial} — бесплатно.", "en": " Your first {trial} are free."},
    "faq.pack": {"ru": ": пакеты от {pack}", "en": ": packs from {pack}"},
    "faq.limits": {
        "ru": "🎬 Какие видео подходят?\nОт {min_s} с до {max_m} мин, где вы говорите на камеру. "
              "Горизонтальное тоже подойдёт — кадрирую в 9:16.",
        "en": "🎬 What videos work?\nFrom {min_s} s to {max_m} min of you talking on camera. "
              "Landscape works too — I'll crop it to 9:16.",
    },
    "faq.tokens": {
        "ru": "🪙 Как считаются токены?\nТокены списываются, когда запускается монтаж: один ролик — "
              "{cost}. Если монтаж не удался, токены возвращаются на баланс.",
        "en": "🪙 How are tokens counted?\nTokens are charged when an edit starts: one reel is "
              "{cost}. If an edit fails, the tokens go back to your balance.",
    },
    "faq.storage": {
        "ru": "🗂 Сколько хранятся готовые ролики?\n{days} — потом исходники и ролики удаляются "
              "с сервера. Сам ролик остаётся у вас в чате.",
        "en": "🗂 How long are finished reels kept?\n{days} — then the sources and reels are "
              "deleted from the server. The reel itself stays in your chat.",
    },
    "faq.storage_forever": {
        "ru": "🗂 Сколько хранятся готовые ролики?\nНе удаляем: ролики остаются и в приложении, и в чате.",
        "en": "🗂 How long are finished reels kept?\nWe don't delete them: they stay in the app and in your chat.",
    },
    "faq.more": {
        "ru": "Не нашли ответ — нажмите «Оператор».",
        "en": "Didn't find your answer? Tap “Operator”.",
    },
    # Контакт поддержки не задан (SUPPORT_CONTACT пуст и у владельца нет
    # @username). Покупателю — без служебных подробностей; владельцу об этом
    # пишет лог (`menu._support_contact`).
    "menu.support_unset": {
        "ru": "Поддержка скоро ответит здесь. А пока загляните в «Мои ролики» — там статус каждого монтажа.",
        "en": "Support will reply here soon. Meanwhile, check “My reels” — each edit's status is there.",
    },
    "help.text": {
        "ru": (
            "Как это работает:\n\n"
            "1. Распознаю речь пословно.\n"
            "2. Решаю, что вырезать и что выделить.\n"
            "3. Режу паузы, кадрирую 9:16, рисую субтитры.\n\n"
            "Команды:\n"
            "/start — начало и выбор языка\n"
            "/language — сменить язык\n"
            "/preset — стиль субтитров\n"
            "/stats — что уже сделано\n\n"
            "Если ролик не получился — пришлите его ещё раз, "
            "а лучше напишите, что именно не так."
        ),
        "en": (
            "How it works:\n\n"
            "1. I transcribe the speech word by word.\n"
            "2. I decide what to cut and what to highlight.\n"
            "3. I cut the pauses, reframe to 9:16 and draw the captions.\n\n"
            "Commands:\n"
            "/start — welcome and language\n"
            "/language — change language\n"
            "/preset — caption style\n"
            "/stats — what's been done\n\n"
            "If a reel didn't turn out right, send it again — "
            "or better, tell us what went wrong."
        ),
    },
    "preset.current": {
        "ru": "Стиль субтитров. Сейчас: «{current}».\nДоступны: {names}",
        "en": "Caption style. Current: “{current}”.\nAvailable: {names}",
    },
    "preset.unknown": {"ru": "Такого стиля нет", "en": "No such style"},
    "preset.picked": {"ru": "Стиль: {title}", "en": "Style: {title}"},
    "ref.bonus": {
        "ru": "🎁 Друг, которого вы пригласили, оплатил пакет. Ваш бонус: +{share}. На балансе: {balance}.",
        "en": "🎁 A friend you invited bought a package. Your bonus: +{share}. Balance: {balance}.",
    },
    "stats.empty": {"ru": "Пока ничего не смонтировано. Пришлите видео.", "en": "Nothing edited yet. Send me a video."},
    "stats.done": {"ru": "Готовых роликов: {n}", "en": "Reels done: {n}"},
    "stats.style": {"ru": "Стиль: {preset}", "en": "Style: {preset}"},
    "stats.balance": {"ru": "На балансе: {n}", "en": "Balance: {n}"},
    # Воронка для владельца. Доля у каждого шага — от предыдущего значимого:
    # «получили ролик» от пришедших, «вернулись» и «заплатили» от получивших,
    # «заплатили ещё раз» от заплативших.
    "stats.funnel_head": {"ru": "\nВоронка за {days} дней:", "en": "\nFunnel, last {days} days:"},
    "stats.funnel_users": {"ru": "  Пришли: {n}", "en": "  Joined: {n}"},
    "stats.funnel_got": {"ru": "  Получили ролик: {n} ({share})", "en": "  Got a reel: {n} ({share})"},
    "stats.funnel_second": {"ru": "  Сделали второй: {n} ({share})", "en": "  Made a second one: {n} ({share})"},
    "stats.funnel_paid": {"ru": "  Заплатили: {n} ({share})", "en": "  Paid: {n} ({share})"},
    "stats.funnel_again": {"ru": "  Заплатили ещё раз: {n} ({share})", "en": "  Paid again: {n} ({share})"},
    "stats.funnel_stars": {"ru": "  Звёзд от них: {n} ⭐", "en": "  Stars from them: {n} ⭐"},
    "stats.cost_head": {"ru": "\nРасход на модель за 7 дней:", "en": "\nModel spend, last 7 days:"},
    "stats.cost_total": {"ru": "  Итого: ${total:.2f}", "en": "  Total: ${total:.2f}"},
    "stats.cost_none": {"ru": "\nВызовов модели пока не было.", "en": "\nNo model calls yet."},

    "fallback.hint": {
        "ru": "Пришлите видео, где вы говорите на камеру, — смонтирую ролик. Стиль и настройки — в приложении.",
        "en": "Send me a video of you talking on camera and I'll edit it into a reel. Style and settings are in the app.",
    },
    "fallback.keyboard_removed": {
        "ru": "Кнопки обновились — теперь всё в меню «Монтаж» и в приложении.",
        "en": "The buttons have changed — everything is now in the “Editor” menu and the app.",
    },

    # ------------------------------------------------------------ приём видео
    "video.not_video": {"ru": "Это не видео. Пришлите видеофайл.", "en": "That's not a video. Please send a video file."},
    "video.one_in_flight": {
        "ru": "Один ролик уже в работе. Дождитесь его — и присылайте следующий.",
        "en": "One reel is already in progress. Wait for it, then send the next one.",
    },
    "video.queue_full": {"ru": "Очередь забита. Попробуйте через несколько минут.", "en": "The queue is full. Try again in a few minutes."},
    "video.too_long": {"ru": "Видео длиннее {max_m} мин. Пришлите кусок покороче.", "en": "The video is longer than {max_m} min. Send a shorter clip."},
    "video.no_balance": {
        "ru": "На балансе не хватает токенов на ролик ({cost}). Пополнить можно в приложении — кнопка ниже.",
        "en": "Not enough tokens for a reel ({cost}). You can top up in the app — button below.",
    },
    "video.accepted": {"ru": "Принял. Ставлю в очередь…", "en": "Got it. Adding to the queue…"},
    "video.accepted_app": {"ru": "Принял видео из приложения. Ставлю в очередь…", "en": "Got your video from the app. Adding to the queue…"},
    "video.too_big": {
        "ru": "Файл больше {mb} МБ — такие Telegram боту не отдаёт. Загрузите видео через приложение (кнопка ниже) или пришлите его как обычное видео, со сжатием.",
        "en": "The file is over {mb} MB, and Telegram won't hand files that big to a bot. Upload it in the app (button below) or send it as a regular, compressed video.",
    },
    "video.download_failed": {
        "ru": "Не смог скачать файл из Telegram. Попробуйте прислать ещё раз.",
        "en": "Couldn't download the file from Telegram. Please try sending it again.",
    },
    "video.done": {"ru": "Готово.", "en": "Done."},
    # Версия ролика из редактора сцен (как у Brandly: «Готова версия 3! ✨»).
    "video.accepted_edit": {"ru": "Собираю версию {n}…", "en": "Building version {n}…"},
    "video.version_ready": {"ru": "Готова версия {n}! ✨", "en": "Version {n} is ready! ✨"},
    "btn.edit": {"ru": "Редактировать", "en": "Edit"},
    # Обложка для ролика.
    "cover.accepted": {"ru": "Рисую обложку…", "en": "Making the cover…"},
    "cover.caption": {
        "ru": "Обложка готова ✨\nФайл в полном размере 1080×1920 — сохраните и поставьте обложкой Reels.",
        "en": "Your cover is ready ✨\nFull-size 1080×1920 file — save it and set it as the Reels cover.",
    },
    "err.cover_failed": {
        "ru": "Не получилось сделать обложку. Токены вернулись на баланс.",
        "en": "Couldn't make the cover. The tokens went back to your balance.",
    },
    "stage.cover": {"ru": "Рисую обложку", "en": "Making the cover"},
    "api.cover_unavailable": {
        "ru": "Для этого ролика обложку сделать нельзя: исходное видео уже удалено с сервера. Загрузите своё фото.",
        "en": "A cover can't be made for this reel: the source video is gone from the server. Upload your own photo.",
    },
    "api.cover_bad": {"ru": "Не получилось принять заказ обложки. Попробуйте ещё раз.",
                      "en": "Couldn't take the cover order. Please try again."},
    "api.no_balance_cover": {"ru": "На балансе не хватает токенов на обложку.",
                             "en": "Not enough tokens for a cover."},
    "video.result_lost": {"ru": "Ролик готов, но файл потерялся. Напишите нам.", "en": "The reel is ready, but the file got lost. Please contact us."},
    "video.not_sent": {
        "ru": "Ролик готов, но в чат не отправился. Он есть в приложении — «Мои ролики».",
        "en": "The reel is ready but couldn't be sent to the chat. It's in the app under “My reels”.",
    },
    "video.caption_durations": {"ru": "{src} с → {res} с", "en": "{src} s → {res} s"},
    "video.caption_saved": {"ru": "вырезано {n} с пауз", "en": "{n} s of pauses cut"},
    "video.caption_style": {"ru": "стиль: {preset}", "en": "style: {preset}"},
    # Что делают кнопки под роликом, сказано один раз в подписи: названий тем
    # шестнадцать, и «Пересобрать в „X“» на каждой кнопке — это стена текста.
    "video.caption_rebuild": {
        "ru": "Тот же монтаж другим стилем:",
        "en": "The same edit in another style:",
    },
    # Человек выбрал музыку из рилса, а в сведение она не попала (трек пропал
    # из хранилища или не читается). Ролик всё равно отдан — молчать об этом
    # нельзя: человек ждал музыку.
    # «Музыка из рилса» в Mini App: почему звук по ссылке не достался.
    "music.bad_link": {"ru": "Не похоже на ссылку. Подойдут Instagram, TikTok или прямая ссылка на аудио- или видеофайл.", "en": "That doesn't look like a link. Instagram, TikTok or a direct link to an audio or video file will do."},
    "music.no_audio": {"ru": "В этом видео нет звука.", "en": "This video has no sound."},
    "music.unavailable": {"ru": "Не получилось открыть: видео закрыто, удалено или недоступно.", "en": "Couldn't open it: the video is private, deleted or unavailable."},
    "music.instagram_down": {"ru": "Сайт не ответил, попробуйте ещё раз.", "en": "The site didn't respond, try again."},
    "music.too_big": {"ru": "Звук по этой ссылке слишком большой.", "en": "The sound at this link is too large."},
    "music.unsupported": {
        "ru": "YouTube пока не поддерживается — пришлите ссылку на рилс, TikTok или файл.",
        "en": "YouTube isn't supported yet — send a reel, TikTok or file link.",
    },
    "music.not_media": {"ru": "По ссылке не аудио и не видео.", "en": "The link isn't an audio or video file."},
    "music.too_many": {
        "ru": "Слишком много ссылок подряд, подождите минуту",
        "en": "Too many links in a row, wait a minute",
    },
    "music.dropped": {
        "ru": "Музыка не успела подготовиться — ролик будет без неё",
        "en": "The music wasn't ready — the video will have none",
    },
    "video.caption_music_lost": {
        "ru": "Музыку из рилса подложить не удалось — ролик без неё.",
        "en": "Couldn't add the reel's music — the video has none.",
    },
    "rerender.no_edl": {"ru": "Исходный монтаж не сохранился", "en": "The original edit wasn't saved"},
    "rerender.purged": {
        "ru": "Исходник удалён по сроку хранения — пересобрать уже нельзя. Пришлите видео заново.",
        "en": "The source was removed after the storage period, so it can't be rebuilt. Please send the video again.",
    },
    "rerender.toast": {"ru": "Пересобираю — без повторного распознавания", "en": "Rebuilding — no need to transcribe again"},
    "rerender.status": {"ru": "Пересобираю в «{preset}»…", "en": "Rebuilding in “{preset}”…"},
    "rerender.busy": {
        "ru": "Один ролик уже пересобирается. Дождитесь его — и выбирайте следующую тему.",
        "en": "One reel is already being rebuilt. Wait for it, then pick the next theme.",
    },

    # ------------------------------------------------------------ стадии монтажа
    "stage.probe": {"ru": "Читаю файл", "en": "Reading the file"},
    "stage.asr": {"ru": "Распознаю речь", "en": "Transcribing speech"},
    "stage.clean": {"ru": "Проверяю расшифровку", "en": "Checking the transcript"},
    "stage.direct": {"ru": "Собираю монтаж", "en": "Planning the edit"},
    "stage.broll": {"ru": "Подбираю картинки", "en": "Picking images"},
    "stage.cut": {"ru": "Режу паузы", "en": "Cutting pauses"},
    "stage.audio": {"ru": "Свожу звук", "en": "Mixing audio"},
    "stage.crop": {"ru": "Кадрирую 9:16", "en": "Reframing to 9:16"},
    "stage.render": {"ru": "Рисую субтитры", "en": "Drawing captions"},
    "stage.done": {"ru": "Готово", "en": "Done"},
    "stage.download": {"ru": "Скачиваю файл", "en": "Downloading the file"},
    "stage.upload": {"ru": "Загружаю результат", "en": "Uploading the result"},
    "stage.join": {"ru": "Склеиваю видео", "en": "Joining the videos"},
    "stage.queued": {"ru": "ждёт своей очереди", "en": "waiting in the queue"},

    # ------------------------------------------------------------ ошибки монтажа
    "err.source_lost": {"ru": "Исходный файл потерялся. Пришлите видео ещё раз.", "en": "The source file got lost. Please send the video again."},
    "err.edl_missing": {"ru": "Монтажный лист не сохранился — пересобрать нечем.", "en": "The edit wasn't saved, so there's nothing to rebuild."},
    "err.too_short": {"ru": "Видео короче {min_s} с — монтировать нечего.", "en": "The video is shorter than {min_s} s — nothing to edit."},
    "err.too_long": {"ru": "Видео длиннее {max_m} мин. Пришлите кусок покороче.", "en": "The video is longer than {max_m} min. Send a shorter clip."},
    "err.no_audio": {"ru": "В видео нет звуковой дорожки — субтитры делать не из чего.", "en": "The video has no audio track, so there's nothing to caption."},
    "err.no_speech": {"ru": "Речь не распознана. Возможно, в ролике только музыка или шум.", "en": "No speech detected. The video may contain only music or noise."},
    "err.edl_invalid": {"ru": "Не удалось собрать монтаж. Попробуйте прислать видео ещё раз.", "en": "Couldn't put the edit together. Please try sending the video again."},
    "err.cut_broken": {"ru": "Нарезка оборвалась — файл повреждён. Пришлите видео ещё раз.", "en": "Cutting was interrupted and the file is damaged. Please send the video again."},
    "err.crop_broken": {"ru": "Кадрирование оборвалось — файл повреждён. Пришлите видео ещё раз.", "en": "Reframing was interrupted and the file is damaged. Please send the video again."},
    "err.worker_lost": {"ru": "Монтаж прервался на нашей стороне — ролик вернулся на баланс. Пришлите видео ещё раз.", "en": "The edit was interrupted on our side — the reel is back on your balance. Please send the video again."},
    "err.failed": {"ru": "Не получилось смонтировать этот ролик. Мы записали ошибку.", "en": "We couldn't edit this video. The error has been logged."},

    # ------------------------------------------------------------ оплата
    "pay.bad_payload": {"ru": "Счёт не распознан. Откройте приложение и выберите пакет заново.", "en": "Invoice not recognised. Open the app and pick a package again."},
    "pay.stars_only": {"ru": "Оплата принимается только звёздами.", "en": "Only Telegram Stars are accepted."},
    "pay.price_changed": {"ru": "Цена изменилась. Откройте приложение и выберите пакет заново.", "en": "The price has changed. Open the app and pick a package again."},
    "pay.unrecognized": {"ru": "Оплата прошла, но пакет не распознан. Напишите нам — зачислим руками.", "en": "Payment went through but the package wasn't recognised. Contact us and we'll credit it manually."},
    "pay.credited": {"ru": "Зачислено: {tokens}. На балансе: {balance}.", "en": "Added {tokens}. Balance: {balance}."},
    "pay.invoice_title": {"ru": "{tokens}", "en": "{tokens}"},
    "pay.invoice_description": {
        "ru": "Монтаж вертикальных роликов: субтитры, нарезка пауз, кадр 9:16.",
        "en": "Vertical reel editing: captions, pause cutting, 9:16 framing.",
    },
    "pay.invoice_label": {"ru": "{tokens}", "en": "{tokens}"},

    # ------------------------------------------------------------ приложение (ответы API)
    "api.empty_file": {"ru": "Пустой файл.", "en": "The file is empty."},
    "api.too_big": {"ru": "Файл больше {mb} МБ.", "en": "The file is larger than {mb} MB."},
    "api.upload_not_found": {"ru": "Загрузка не найдена — начните заново.", "en": "Upload not found — please start again."},
    "api.too_many_clips": {"ru": "В одном ролике — не больше {n} видео.", "en": "One reel takes up to {n} videos."},
    "api.clips_with_split": {
        "ru": "В режиме «Два видео» снизу и сверху — по одному видео.",
        "en": "In “Two videos” mode it's one video on top and one below.",
    },
    "api.clip_unreadable": {
        "ru": "Не получилось прочитать видео №{n}. Пришлите mp4 или mov.",
        "en": "Couldn't read video #{n}. Please upload an mp4 or mov.",
    },
    "api.too_many_uploads": {"ru": "Больше {n} файлов разом загружать нельзя — дождитесь или начните заново.", "en": "You can't upload more than {n} files at once — wait for them or start over."},
    "api.no_space": {"ru": "На сервере не хватает места под этот файл. Попробуйте позже или пришлите короче.", "en": "The server is out of space for this file. Try later or send a shorter one."},
    "api.bad_offset": {"ru": "Некорректный сдвиг загрузки.", "en": "Invalid upload offset."},
    "api.chunk_too_far": {"ru": "Кусок выходит за размер файла.", "en": "The chunk goes past the end of the file."},
    "api.offset_mismatch": {"ru": "Сдвиг не совпал.", "en": "Offset mismatch."},
    "api.incomplete": {"ru": "Файл загружен не полностью.", "en": "The file hasn't been fully uploaded."},
    "api.unknown_style": {"ru": "Такого стиля нет.", "en": "No such style."},
    "api.blocked": {"ru": "Доступ закрыт.", "en": "Access denied."},
    "api.top_unreadable": {"ru": "Не получилось прочитать верхнее видео. Пришлите mp4 или mov.", "en": "Couldn't read the top video. Please upload an mp4 or mov."},
    "api.unreadable": {"ru": "Не получилось прочитать видео. Пришлите mp4 или mov.", "en": "Couldn't read the video. Please upload an mp4 or mov."},
    "api.no_balance": {"ru": "На балансе не хватает токенов на ролик.", "en": "Not enough tokens for a reel."},
    "api.enqueue_failed": {"ru": "Не получилось поставить ролик в очередь. Баланс не списан.", "en": "Couldn't queue the video. Your balance was not charged."},
    "api.video_not_found": {"ru": "Ролик не найден.", "en": "Video not found."},
    # Редактор сцен.
    "api.edit_unavailable": {
        "ru": "Этот ролик нельзя открыть в редакторе.",
        "en": "This reel can't be opened in the editor.",
    },
    "api.edit_source_gone": {
        "ru": "Исходное видео удалено с сервера по сроку хранения — собрать новую версию не из чего.",
        "en": "The source video was removed after the storage period, so a new version can't be built.",
    },
    "api.edit_bad": {"ru": "Правки не применились. Обновите редактор и попробуйте ещё раз.",
                     "en": "The edits couldn't be applied. Reopen the editor and try again."},
    "api.edit_text_long": {"ru": "Текст сцены слишком длинный.", "en": "The scene text is too long."},
    "api.edit_empty": {"ru": "Так в ролике ничего не останется — оставьте хотя бы одно слово.",
                       "en": "That would leave nothing in the reel — keep at least one word."},
    "api.edit_media_close": {
        "ru": "Картинки стоят слишком близко друг к другу — уберите одну из соседних.",
        "en": "The pictures are too close together — remove one of the neighbours.",
    },
    "api.edit_nothing": {"ru": "Вы ничего не поменяли.", "en": "Nothing was changed."},
    "api.media_bad": {"ru": "Не получилось прочитать картинку. Подойдут JPG и PNG до 8 МБ.",
                      "en": "Couldn't read the picture. JPG and PNG up to 8 MB work."},
    "api.no_balance_edit": {"ru": "На балансе не хватает токенов на новую версию.",
                            "en": "Not enough tokens for a new version."},
    "api.not_ready": {"ru": "Ролик ещё не готов.", "en": "The video isn't ready yet."},
    "api.purged": {
        "ru": "Ролик удалён с сервера по сроку хранения. В чате он остался.",
        "en": "The video was removed from the server after the storage period. It's still in your chat.",
    },
    "api.resend_failed": {
        "ru": "Не получилось отправить в чат. Напишите боту /start и попробуйте ещё раз.",
        "en": "Couldn't send it to the chat. Send /start to the bot and try again.",
    },
    "api.no_package": {"ru": "Не выбран пакет.", "en": "No package selected."},
    "api.unknown_package": {"ru": "Такого пакета нет.", "en": "No such package."},
    "api.unknown_method": {"ru": "Этот способ оплаты недоступен.", "en": "This payment method isn't available."},
    "api.below_minimum": {
        "ru": "Этим способом можно оплатить пакет от {rub} ₽. Выберите пакет побольше или другой способ.",
        "en": "This method takes packages from {rub} ₽. Pick a bigger package or another method.",
    },
    "api.payhot_failed": {
        "ru": "Платёжная система не ответила. Попробуйте ещё раз или выберите другой способ.",
        "en": "The payment system didn't respond. Try again or pick another method.",
    },
    "api.order_not_found": {"ru": "Заказ не найден.", "en": "Order not found."},
    "api.too_many_orders": {
        "ru": "Слишком много счетов подряд. Оплатите открытый или подождите несколько минут.",
        "en": "Too many invoices in a row. Pay the open one or wait a few minutes.",
    },
    "api.method_not_for_package": {
        "ru": "Этим способом этот пакет не оплатить. Выберите пакет побольше или другой способ.",
        "en": "This package can't be paid this way. Pick a bigger package or another method.",
    },
    "api.link_expired": {"ru": "Ссылка устарела.", "en": "The link has expired."},
    "api.font_not_found": {"ru": "Шрифт не найден.", "en": "Font not found."},
}


def stage_title(key: str | None, lang: str | None) -> str | None:
    if not key:
        return None
    return t(f"stage.{key}", lang) if f"stage.{key}" in TEXTS else None


def error_text(code: str | None, lang: str | None, *, min_s: float, max_m: float) -> str:
    code = code if code in ERROR_CODES else "failed"
    return t(f"err.{code}", lang, min_s=f"{min_s:.0f}", max_m=f"{max_m:.0f}")
