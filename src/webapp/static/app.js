/* Mini App. Без сборки и фреймворка: семь экранов, история переходов, API на том же хосте. */
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// Тексты интерфейса. Язык приходит с сервера (`user.lang`) и меняется в шапке и в профиле.
const I18N = {
  ru: {
    theme: "Сменить тему", back: "Назад", forward: "Вперёд", close: "Закрыть",
    "nav.home": "Главная", "nav.invite": "Пригласить", "nav.studio": "Студия", "nav.edit": "Монтаж", "nav.videos": "Результаты",
    "nav.tariffs": "Тарифы", "nav.profile": "Профиль", "nav.collapse": "Свернуть панель",
    "title.invite": "Приглашайте друзей", "step": "Шаг {n} из 2",
    "home.new": "Новый ролик", "home.new_sub": "Загрузите видео — смонтируем за пару минут",
    "home.recent": "Последнее", "home.all": "Все", "home.invite": "Пригласите друга",
    "home.invite_sub": "Токены вам — с каждой его покупки",
    "studio.new_sub": "Из видео — готовый Reels",
    "notif.title": "Уведомления", "notif.empty": "Пока нет уведомлений",
    "notif.done": "Ролик готов", "notif.failed": "Ролик не получился", "notif.failed_sub": "Токены вернулись на баланс",
    "home.empty": "Роликов пока нет", "home.empty_sub": "Первый ролик появится здесь",
    "preview.pause": "Пауза", "preview.play": "Воспроизвести",
    "preview.s1": ["это", "пример", "субтитров", "в", "стиле"],
    "preview.s2": ["паузы", "уже", "вырезаны"],
    "preview.s3": ["и", "кадр", "по центру", "лица"],
    "edit.s1_title": "Загрузите видео для ролика",
    "edit.s1_lead": "Можно одно или несколько — склеим их в один Reels в том порядке, который вы зададите.",
    "edit.s1_lead_one": "Видео, где человек говорит в камеру. Соберём из него вертикальный ролик с субтитрами.",
    "edit.drop_many": "Сразу несколько — тоже можно",
    "edit.limits": "до {mb} МБ · от {min} с до {max} мин",
    "edit.style": "Стиль субтитров", "edit.font": "Шрифт", "edit.color": "Цвет выделения",
    "edit.sound": "Звук", "edit.frame": "Кадр", "edit.graphics": "Графика",
    "card.captions": "Субтитры",
    "music.title": "Музыка по ссылке", "music.placeholder": "Ссылка на рилс, TikTok или файл", "music.paste": "Вставить",
    "music.checking": "Достаём звук…", "music.ready": "Трек {t}", "music.clear": "Убрать музыку",
    "music.hint": "Звук по ссылке тихо заиграет под вашей речью",
    "music.dropped": "Музыка не успела подготовиться — ролик будет без неё",
    "edit.requirements": "Требования к видео", "edit.in_chat": "Загрузить в боте",
    "edit.untitled": "Видео", "edit.remove": "Убрать видео",
    "edit.next": "Далее: оформление", "edit.next_idle": "Сначала добавьте видео", "edit.next_ready": "стиль, шрифт и цвет субтитров",
    "edit.back_to_video": "← Вернуться к видео",
    "opt.cut": "Нарезка", "cut.on": "Вырезать паузы", "cut.off": "Без нарезки",
    "cut.on_hint": "Уберём затяжные паузы и долгие «эээ» — ролик станет плотнее.",
    "cut.off_hint": "Видео пойдёт целиком, как снято: только кадр 9:16 и субтитры.",
    "opt.sfx": "Звуковые эффекты", "opt.broll": "Медиа-карточки", "opt.camera": "Движение камеры",
    "opt.studio": "Студийный звук", "opt.tracking": "Слежение за головой", "opt.transitions": "Переходы",
    "opt.grade": "Цветокоррекция", "opt.smooth": "Плавность 60 к/с", "opt.visuals": "Графика и стикеры",
    "opt.title": "Заголовок в начале", "opt.progress": "Полоса прогресса", "opt.music": "Фоновая музыка",
    "help.music": ["Фоновая музыка", "ИИ подбирает спокойный трек под смысл ролика и тихо кладёт его под голос. Если вы вставили свою музыку по ссылке, играет она."],
    "help.cut": ["Нарезка", "Вырезаем затяжные паузы дольше 1.2 секунды и долгие «эээ». Обычные вдохи и слова-паразиты остаются — так речь звучит естественно."],
    "help.sfx": ["Звуковые эффекты", "«Whoosh» на появлении медиа-карточки, при смене крупности и на въезде кадра сбоку. Громкость голоса выравнивается в любом случае."],
    "help.studio": ["Студийный звук", "Убираем гул и шипение, приглушаем свистящие, добавляем голосу разборчивости и плотности. Выключите, если хотите оставить звук как записан, — тогда выровняем только громкость."],
    "help.tracking": ["Слежение за головой", "Кадр плавно едет за вами, когда вы двигаетесь, и лицо остаётся в центре. Выключите, если снимаете со штатива и хотите неподвижный кадр: он встанет по вашему обычному положению."],
    "help.transitions": ["Переходы", "На склейках и ударных словах кадр на треть секунды наезжает, въезжает сбоку или вспыхивает — рез читается как приём, а не как рывок. Выключите — склейки останутся жёсткими."],
    "help.grade": ["Цветокоррекция", "Чуть больше контраста и насыщенности, чтобы видео с телефона не выглядело тёмным и плоским; в части стилей — лёгкое затемнение по краям. Выключите, если уже обработали цвет сами."],
    "help.smooth": ["Плавность 60 кадров/с", "Кадр, субтитры и графика двигаются вдвое плавнее. Выключите — ролик останется в частоте исходника (обычно 30 кадров/с), а монтаж пойдёт быстрее."],
    "help.visuals": ["Графика и стикеры", "Плашки с цифрами и ценами, стикеры и подчёркивания по смыслу сказанного. Выключите — плашек и стикеров в ролике не будет."],
    "help.title": ["Заголовок в начале", "В первые секунды поверх видео появляется крупный заголовок ролика — его придумывает ИИ по вашей речи. Есть не во всех стилях субтитров."],
    "help.progress": ["Полоса прогресса", "Тонкая линия вверху кадра заполняется по ходу ролика: зрителю видно, сколько осталось до конца."],
    "help.broll": ["Медиа-карточки", "Картинки и короткие клипы по смыслу сказанного — не чаще одной на 8 секунд и никогда поверх субтитров."],
    "help.camera": ["Движение камеры", "На резах кадр чередует общий и крупный план, чтобы склейки читались как монтаж. Выключите — весь ролик пойдёт одним планом."],
    "req.title": "Требования к видео",
    "req.duration": "Длительность от {min} с до {max} мин.", "req.size": "Размер до {mb} МБ.",
    "req.speaker": "В кадре — говорящий человек, звук с речью.",
    "req.orientation": "Подойдёт и вертикальное, и горизонтальное: кадр 9:16 соберём сами.",
    "req.clips": "Можно до {n} видео: склеим по порядку в один ролик. Длина считается по всем вместе.",
    "clips.title": "Ваши видео · {n}", "clips.total": "всего {t}", "clips.add": "Добавить ещё видео",
    "clips.max": "В одном ролике — не больше {n} видео.",
    "file.too_long": "Видео длиннее {max} мин — сервер его не примет.",
    "file.too_short": "Видео короче {min} с — монтировать нечего.",
    "size.mb": "МБ", "size.gb": "ГБ", "dur.s": "с",
    "cta.edit": "Смонтировать", "cta.choose": "Выбрать видео",
    "layout.single": "Одно видео", "layout.split": "Два видео",
    "split.top_title": "Сверху — что показываем", "split.top_sub": "без звука",
    "split.bottom_title": "Снизу — вы в кадре", "split.bottom_sub": "звук и субтитры отсюда",
    "split.need_both": "добавьте оба видео", "split.top_role": "Сверху", "split.bottom_role": "Снизу",
    "edit.s1_lead_split": "Сверху — то, что показываете, снизу — вы в кадре. Соберём из двух видео ролик с субтитрами.",
    "cta.topup": "Пополнить баланс", "cta.missing": "на ролик не хватает {n}",
    "cta.free": "бесплатно для владельца", "cta.ready": "готовый ролик придёт в чат", "cta.pick": "сначала выберите видео", "cta.charge": "спишется {c} из {n}",
    "cta.uploading": "Загрузка {p}%", "cta.checking": "Проверяю видео…", "cta.keep_open": "не закрывайте приложение до конца загрузки",
    "upload.lost": "Связь оборвалась во время загрузки. Попробуйте ещё раз.",
    "toast.accepted": "Видео принято. Готовый ролик придёт в чат, а прогресс — в «Результатах».",
    "err.title": "Не получилось",
    "balance.unlimited": "без ограничений",
    "balance.note": "Один ролик — {c}. Если монтаж не получится, токены вернутся на баланс.",
    "balance.note_admin": "Вы владелец бота: монтаж бесплатный, баланс не списывается.",
    "status.queued": "в очереди", "status.running": "монтируется", "status.done": "готов", "status.failed": "не получилось", "status.cancelled": "отменён",
    "jobs.all": "Все", "jobs.done": "Готовые", "jobs.active": "В работе",
    "jobs.empty": "Роликов пока нет", "jobs.empty_hint": "Загрузите видео — готовый ролик придёт в чат и появится здесь.",
    "jobs.empty_filter": "Здесь пока пусто",
    "jobs.to_edit": "К монтажу", "jobs.reel": "Монтаж", "jobs.refunded": "Токены вернулись на баланс.",
    "jobs.keep": "хранится до {d}", "jobs.purged_short": "удалён с сервера",
    "jobs.purged": "Удалён с сервера по сроку хранения. В чате ролик остался.",
    "jobs.send": "Прислать в чат", "jobs.sent": "Отправил в чат.", "jobs.not_sent": "Не отправилось",
    "tariffs.balance": "Баланс", "tariffs.history": "История", "tariffs.package": "Пакет", "tariffs.method": "Способ оплаты",
    "pkg.per": "до {n} {w}", "pkg.rub": "≈ {n} ₽", "pkg.popular": "популярный", "pkg.save": "выгода −{n}%",
    "pay.button": "Оплатить {n} ⭐", "pay.button_rub": "Оплатить {n} ₽", "pay.sub": "{n} {w} на баланс",
    "method.applepay": "Apple Pay", "method.googlepay": "Google Pay",
    "method.card": "Visa / Mastercard (Европа) 🌍", "method.sbp": "СБП 💳", "method.crypto": "Криптовалюта 🪙",
    "method.stars": "Telegram Stars ⭐", "method.from": "от {n} ₽",
    "pay.opening": "Открываю оплату…", "pay.waiting": "Ждём оплату — после оплаты вернитесь в приложение",
    "pay.not_paid": "Оплата не прошла. Деньги не списаны — попробуйте ещё раз или другим способом.",
    "pay.title": "Оплата", "pay.update": "Обновите Telegram — эта версия не умеет оплату звёздами.",
    "pay.failed": "Оплата не прошла", "pay.failed_text": "Звёзды не списаны. Попробуйте ещё раз.",
    "pay.crediting": "Оплачено. Зачисляю…", "pay.credited": "Зачислено: {n} {w}.", "pay.later": "Оплата прошла, баланс обновится через минуту.",
    "history.title": "История", "history.empty": "Оплат пока не было",
    "history.payment": "Покупка пакета", "history.bonus": "Бонус за друга",
    "invite.lead": "За каждую оплату друга вам возвращается {p}% от купленных им токенов — прямо на баланс.",
    "invite.your_link": "Ваша ссылка", "invite.copy": "Скопировать", "invite.share": "Поделиться",
    "invite.copied": "Ссылка скопирована",
    "invite.all_time": "За всё время", "invite.came": "Пришли", "invite.paid": "Оплатили", "invite.earned": "Токенов",
    "invite.today": "+{n} сегодня",
    "invite.credit": "Накоплено {n} токена — на баланс уйдёт целым токеном, как только наберётся.",
    "invite.friends": "Приглашённые", "invite.sum_came": "пришли", "invite.sum_paid": "оплатили", "invite.sum_tokens": "токенов",
    "invite.none_period": "За этот период по вашей ссылке никто не зарегистрировался.",
    "invite.none_all": "По вашей ссылке пока никто не зарегистрировался.",
    "invite.friend_paid": "оплатил", "invite.friend_waiting": "ещё не покупал", "invite.noname": "Без имени",
    "period.7": "7 дней", "period.30": "30 дней", "period.0": "Всё время", "period.title": "Период",
    "partner.title": "Деньги вместо токенов", "partner.terms": "Условия", "partner.become": "Стать партнёром",
    "partner.sub": "{p}% с оплат приглашённых — рублями на реквизиты, для самозанятых и ИП.",
    "partner.payout": "Выплата", "partner.payout_foot": "{p}% · на карту",
    "partner.row_sub": "{p}% рублями — для самозанятых и ИП",
    "partner.terms_text": "Партнёрам — самозанятым и ИП — платим за приглашённых деньгами, а не токенами: {p}% с каждой их оплаты, рублями на реквизиты. Нажмите «Стать партнёром» и напишите нам — расскажем условия и оформим партнёрство.",
    "partner.soon": "Партнёрская программа для самозанятых и ИП скоро откроется — следите за новостями.",
    "invite.text": "Монтирую вертикальные ролики за пару минут: субтитры, паузы, кадр 9:16.",
    "profile.noname": "Без имени",
    "profile.language": "Язык", "profile.theme": "Тема",
    "profile.invite": "Пригласить друга", "profile.invite_sub": "{p}% с каждой оплаты — вам на баланс",
    "profile.login": "Вход и уведомления", "profile.channel": "Официальный канал", "profile.support": "Поддержка",
    "profile.offer": "Публичная оферта",
    "profile.hint_admin": "Вы в списке владельцев: монтаж без списаний. Чтобы проверить оплату как обычный пользователь, уберите свой id из ADMIN_IDS.",
    "blocker.title": "Откройте из Telegram", "blocker.text": "Приложение работает внутри бота: нажмите кнопку «Приложение» в чате.",
    "blocker.failed": "Не получилось открыть",
  },
  en: {
    theme: "Toggle theme", back: "Back", forward: "Forward", close: "Close",
    "nav.home": "Home", "nav.invite": "Invite", "nav.studio": "Studio", "nav.edit": "Editor", "nav.videos": "Results",
    "nav.tariffs": "Pricing", "nav.profile": "Profile", "nav.collapse": "Collapse sidebar",
    "title.invite": "Invite friends", "step": "Step {n} of 2",
    "home.new": "New reel", "home.new_sub": "Upload a video — we'll edit it in a couple of minutes",
    "home.recent": "Recent", "home.all": "All", "home.invite": "Invite a friend",
    "home.invite_sub": "Tokens for you — from every purchase they make",
    "studio.new_sub": "From a video to a finished Reel",
    "notif.title": "Notifications", "notif.empty": "No notifications yet",
    "notif.done": "Your reel is ready", "notif.failed": "The reel failed", "notif.failed_sub": "The tokens went back to your balance",
    "home.empty": "No videos yet", "home.empty_sub": "Your first reel will show up here",
    "preview.pause": "Pause", "preview.play": "Play",
    "preview.s1": ["this", "is", "captions", "in", "style"],
    "preview.s2": ["pauses", "are", "gone"],
    "preview.s3": ["and", "the frame", "follows", "the face"],
    "edit.s1_title": "Upload a video for your reel",
    "edit.s1_lead": "One video or several — we'll join them into one Reel in the order you set.",
    "edit.s1_lead_one": "A video of someone talking to the camera. We'll turn it into a vertical reel with captions.",
    "edit.drop_many": "Several at once works too",
    "edit.limits": "up to {mb} MB · {min} s to {max} min",
    "edit.style": "Caption style", "edit.font": "Typeface", "edit.color": "Highlight colour",
    "edit.sound": "Sound", "edit.frame": "Frame", "edit.graphics": "Graphics",
    "card.captions": "Captions",
    "music.title": "Music from a link", "music.placeholder": "Reel, TikTok or file link", "music.paste": "Paste",
    "music.checking": "Getting the sound…", "music.ready": "Track {t}", "music.clear": "Remove music",
    "music.hint": "The sound plays quietly under your voice",
    "music.dropped": "The music wasn't ready — the video will have none",
    "edit.requirements": "Video requirements", "edit.in_chat": "Upload in the bot",
    "edit.untitled": "Video", "edit.remove": "Remove video",
    "edit.next": "Next: design", "edit.next_idle": "Add a video first", "edit.next_ready": "caption style, typeface and colour",
    "edit.back_to_video": "← Back to the video",
    "opt.cut": "Cutting", "cut.on": "Cut pauses", "cut.off": "No cutting",
    "cut.on_hint": "We'll remove long pauses and drawn-out “umm”s — the reel gets tighter.",
    "cut.off_hint": "The video stays whole, as shot: just the 9:16 frame and captions.",
    "opt.sfx": "Sound effects", "opt.broll": "Media cards", "opt.camera": "Camera movement",
    "opt.studio": "Studio sound", "opt.tracking": "Head tracking", "opt.transitions": "Transitions",
    "opt.grade": "Colour correction", "opt.smooth": "Smooth 60 fps", "opt.visuals": "Graphics and stickers",
    "opt.title": "Opening title", "opt.progress": "Progress bar", "opt.music": "Background music",
    "help.music": ["Background music", "AI picks a calm track that fits the reel and plays it quietly under your voice. If you added your own music by link, that one plays instead."],
    "help.cut": ["Cutting", "We remove pauses longer than 1.2 seconds and long “umm”s. Normal breaths and filler words stay, so the speech sounds natural."],
    "help.sfx": ["Sound effects", "A “whoosh” when a media card appears, when the shot size changes and when the frame slides in. Voice loudness is levelled either way."],
    "help.studio": ["Studio sound", "We remove hum and hiss, soften harsh “s” sounds and make the voice clearer and fuller. Turn it off to keep the sound as recorded — then we only level the loudness."],
    "help.tracking": ["Head tracking", "The frame follows you smoothly as you move, keeping your face centred. Turn it off if you shoot on a tripod and want a still frame: it settles on your usual position."],
    "help.transitions": ["Transitions", "At cuts and on stressed words the frame punches in, slides in or flashes for a third of a second — the cut reads as a move, not a jolt. Turn it off to keep hard cuts."],
    "help.grade": ["Colour correction", "A touch more contrast and saturation so phone footage doesn't look dark and flat; some styles add a slight vignette. Turn it off if you've already graded the video."],
    "help.smooth": ["Smooth 60 fps", "The frame, captions and graphics move twice as smoothly. Turn it off to keep the source frame rate (usually 30 fps) — editing gets faster."],
    "help.visuals": ["Graphics and stickers", "Number and price chips, stickers and underlines that match what's being said. Turn it off for a reel without chips and stickers."],
    "help.title": ["Opening title", "A large title appears over the video in the first seconds — AI writes it from your speech. Not every caption style has one."],
    "help.progress": ["Progress bar", "A thin line at the top fills as the reel plays, so viewers see how much is left."],
    "help.broll": ["Media cards", "Images and short clips that match what's being said — at most one per 8 seconds and never over the captions."],
    "help.camera": ["Camera movement", "At cuts the frame alternates between wide and close-up so the edits read as editing. Turn it off to keep one shot throughout."],
    "req.title": "Video requirements",
    "req.duration": "Length from {min} s to {max} min.", "req.size": "Up to {mb} MB.",
    "req.speaker": "A person talking on camera, with audible speech.",
    "req.orientation": "Portrait or landscape — we'll build the 9:16 frame ourselves.",
    "req.clips": "Up to {n} videos: we join them in order into one reel. The length is counted across all of them.",
    "clips.title": "Your videos · {n}", "clips.total": "{t} total", "clips.add": "Add another video",
    "clips.max": "One reel takes up to {n} videos.",
    "file.too_long": "The video is longer than {max} min — the server won't accept it.",
    "file.too_short": "The video is shorter than {min} s — nothing to edit.",
    "size.mb": "MB", "size.gb": "GB", "dur.s": "s",
    "cta.edit": "Create reel", "cta.choose": "Choose a video",
    "layout.single": "One video", "layout.split": "Two videos",
    "split.top_title": "Top — what you show", "split.top_sub": "muted",
    "split.bottom_title": "Bottom — you on camera", "split.bottom_sub": "sound and captions come from here",
    "split.need_both": "add both videos", "split.top_role": "Top", "split.bottom_role": "Bottom",
    "edit.s1_lead_split": "On top — what you show, below — you on camera. We'll turn the two videos into a reel with captions.",
    "cta.topup": "Top up balance", "cta.missing": "{n} short of a reel",
    "cta.free": "free for the owner", "cta.ready": "the finished reel arrives in the chat", "cta.pick": "pick a video first", "cta.charge": "uses {c} of your {n}",
    "cta.uploading": "Uploading {p}%", "cta.checking": "Checking the video…", "cta.keep_open": "keep the app open until the upload finishes",
    "upload.lost": "The connection dropped during upload. Please try again.",
    "toast.accepted": "Video received. The finished reel will arrive in the chat; track progress in “Results”.",
    "err.title": "Something went wrong",
    "balance.unlimited": "unlimited",
    "balance.note": "One reel is {c}. If editing fails, the tokens go back to your balance.",
    "balance.note_admin": "You own this bot: editing is free and your balance isn't used.",
    "status.queued": "queued", "status.running": "editing", "status.done": "ready", "status.failed": "failed", "status.cancelled": "cancelled",
    "jobs.all": "All", "jobs.done": "Ready", "jobs.active": "In progress",
    "jobs.empty": "No videos yet", "jobs.empty_hint": "Upload a video — the finished reel will arrive in the chat and show up here.",
    "jobs.empty_filter": "Nothing here yet",
    "jobs.to_edit": "Go to editor", "jobs.reel": "Edit", "jobs.refunded": "The tokens went back to your balance.",
    "jobs.keep": "kept until {d}", "jobs.purged_short": "removed from the server",
    "jobs.purged": "Removed from the server after the storage period. It's still in your chat.",
    "jobs.send": "Send to chat", "jobs.sent": "Sent to the chat.", "jobs.not_sent": "Couldn't send",
    "tariffs.balance": "Balance", "tariffs.history": "History", "tariffs.package": "Package", "tariffs.method": "Payment method",
    "pkg.per": "up to {n} {w}", "pkg.rub": "≈ {n} ₽", "pkg.popular": "popular", "pkg.save": "save {n}%",
    "pay.button": "Pay {n} ⭐", "pay.button_rub": "Pay {n} ₽", "pay.sub": "{n} {w} to your balance",
    "method.applepay": "Apple Pay", "method.googlepay": "Google Pay",
    "method.card": "Visa / Mastercard (Europe) 🌍", "method.sbp": "SBP 💳", "method.crypto": "Crypto 🪙",
    "method.stars": "Telegram Stars ⭐", "method.from": "from {n} ₽",
    "pay.opening": "Opening the payment page…", "pay.waiting": "Waiting for the payment — come back to the app after paying",
    "pay.not_paid": "The payment didn't go through. Nothing was charged — try again or another method.",
    "pay.title": "Payment", "pay.update": "Please update Telegram — this version can't pay with Stars.",
    "pay.failed": "Payment failed", "pay.failed_text": "No Stars were charged. Please try again.",
    "pay.crediting": "Paid. Adding to your balance…", "pay.credited": "Added: {n} {w}.", "pay.later": "Payment received; your balance will update in a minute.",
    "history.title": "History", "history.empty": "No payments yet",
    "history.payment": "Package purchase", "history.bonus": "Friend bonus",
    "invite.lead": "Every time a friend pays, {p}% of the tokens they bought comes back to you — straight to your balance.",
    "invite.your_link": "Your link", "invite.copy": "Copy", "invite.share": "Share",
    "invite.copied": "Link copied",
    "invite.all_time": "All time", "invite.came": "Joined", "invite.paid": "Paid", "invite.earned": "Tokens",
    "invite.today": "+{n} today",
    "invite.credit": "{n} of a token saved up — it moves to your balance as a whole token once it adds up.",
    "invite.friends": "Invited", "invite.sum_came": "joined", "invite.sum_paid": "paid", "invite.sum_tokens": "tokens",
    "invite.none_period": "Nobody signed up with your link in this period.",
    "invite.none_all": "Nobody has signed up with your link yet.",
    "invite.friend_paid": "paid", "invite.friend_waiting": "hasn't bought yet", "invite.noname": "No name",
    "period.7": "7 days", "period.30": "30 days", "period.0": "All time", "period.title": "Period",
    "partner.title": "Money instead of tokens", "partner.terms": "Terms", "partner.become": "Become a partner",
    "partner.sub": "{p}% of what your invited friends pay — in roubles to your bank details, for the self-employed and sole traders.",
    "partner.payout": "Payout", "partner.payout_foot": "{p}% · to your card",
    "partner.row_sub": "{p}% in roubles — for the self-employed and sole traders",
    "partner.terms_text": "Self-employed partners and sole traders get paid in money, not tokens: {p}% of every payment their invited friends make, in roubles. Tap “Become a partner” and write to us — we'll set it up.",
    "partner.soon": "The partner programme for the self-employed and sole traders is opening soon.",
    "invite.text": "I turn talking-head videos into vertical reels in a couple of minutes: captions, pauses cut, 9:16 frame.",
    "profile.noname": "No name",
    "profile.language": "Language", "profile.theme": "Theme",
    "profile.invite": "Invite a friend", "profile.invite_sub": "{p}% of every payment — to your balance",
    "profile.login": "Sign-in and notifications", "profile.channel": "Official channel", "profile.support": "Support",
    "profile.offer": "Public offer",
    "profile.hint_admin": "You're on the owners list: editing is free. To test payments as a regular user, remove your id from ADMIN_IDS.",
    "blocker.title": "Open from Telegram", "blocker.text": "This app runs inside the bot: tap the “App” button in the chat.",
    "blocker.failed": "Couldn't open the app",
  },
};

const SCREENS = ["home", "invite", "studio", "edit", "videos", "tariffs", "profile"];
// Вкладки нижней панели. У них в шапке логотип, тема, уведомления и баланс;
// у вложенных экранов (монтаж, тарифы) — «назад» и название.
const ROOT_SCREENS = ["home", "invite", "studio", "videos", "profile"];
// Какая вкладка подсвечена на вложенном экране: монтаж открывают из «Студии».
const NAV_OF = { edit: "studio" };
// Старые имена вкладок: бот присылает кнопки с `?screen=balance`.
const SCREEN_ALIASES = { balance: "tariffs" };

function currentLang() {
  if (state.boot && state.boot.user && I18N[state.boot.user.lang]) return state.boot.user.lang;
  const code = ((tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code) || navigator.language || "").toLowerCase();
  return ["ru", "uk", "be", "kk", "uz", "ky", "tg", "hy", "az"].includes(code.split("-")[0]) ? "ru" : "en";
}

function tr(key, params) {
  const lang = currentLang();
  let text = (I18N[lang] && I18N[lang][key]) ?? I18N.ru[key] ?? key;
  if (typeof text === "string" && params) {
    for (const [k, v] of Object.entries(params)) text = text.replaceAll(`{${k}}`, v);
  }
  return text;
}

function applyStaticTexts() {
  document.documentElement.lang = currentLang();
  for (const node of $$("[data-i18n]")) node.textContent = tr(node.dataset.i18n);
  for (const node of $$("[data-i18n-aria]")) node.setAttribute("aria-label", tr(node.dataset.i18nAria));
  for (const node of $$("[data-i18n-placeholder]")) node.placeholder = tr(node.dataset.i18nPlaceholder);
  if (state.music) renderMusic();
}

const state = {
  boot: null,
  preset: null,
  accent: "theme",
  options: {},
  file: null,
  // «Два видео» (`pipeline.split`): `file` — нижнее, говорящий; `topFile` —
  // верхнее, то, что показывают.
  layout: "single",
  topFile: null,
  // Ролик из нескольких видео: куски в порядке склейки, `{ file, duration,
  // thumb }`. Первый кусок — он же `file`; в «Двух видео» кусок всегда один.
  clips: [],
  // «Музыка из рилса»: ссылку проверяет сервер (`POST /api/music`), `id` уходит
  // в `finish`. `seq` отбрасывает ответы на ссылки, которые уже заменили.
  music: { status: "empty", id: null, duration: null, error: null, seq: 0, url: "" },
  busy: false,
  jobs: [],
  jobsLoaded: false,
  jobsFilter: "all",
  pollTimer: null,
  screen: null,
  history: [],
  step: 1,
  pkg: null,
  payMethod: null,
  refDays: 30,
  refs: null,
};

// ------------------------------------------------------------------ сеть

async function api(path, { method = "GET", body, raw } = {}) {
  const headers = { Authorization: `tma ${tg ? tg.initData : ""}` };
  let payload = body;
  if (body !== undefined && !raw) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(path, { method, headers, body: payload });
  let data = {};
  try { data = await res.json(); } catch (_) { /* пустой ответ */ }
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// ------------------------------------------------------------------ мелочи

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  if (a >= 11 && a <= 14) return many;
  return { 1: one, 2: few, 3: few, 4: few }[a % 10] || many;
}
const videosWord = (n) => (currentLang() === "en"
  ? (Math.abs(n) === 1 ? "video" : "videos")
  : plural(n, "ролик", "ролика", "роликов"));
const tokensWord = (n) => (currentLang() === "en"
  ? (Math.abs(n) === 1 ? "token" : "tokens")
  : plural(n, "токен", "токена", "токенов"));
// «240 токенов». Баланс — в токенах, ролик стоит `boot.video_cost`.
const tokensText = (n) => `${n} ${tokensWord(n)}`;
const videoCost = () => (state.boot && state.boot.video_cost) || 1;
const fmtRub = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");

// Сотые ролика → «0,5» / «0.5». Бонус за друга копится дробью: 10 % от пакета
// в 5 роликов — половина ролика.
function fmtShare(hundredths) {
  const n = (hundredths || 0) / 100;
  const text = Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/0+$/, "");
  return currentLang() === "ru" ? text.replace(".", ",") : text;
}

function locale() { return currentLang() === "en" ? "en-GB" : "ru-RU"; }
function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleString(locale(), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function fmtDate(iso) {
  return iso ? new Date(iso).toLocaleDateString(locale(), { day: "numeric", month: "short" }) : "";
}

function haptic(kind = "selection") {
  if (!tg || !tg.HapticFeedback) return;
  try {
    if (kind === "selection") tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.notificationOccurred(kind);
  } catch (_) { /* старый клиент */ }
}

let toastTimer = null;
function toast(text) {
  const node = $("#toast");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
}

function popup(title, message) {
  if (tg && tg.showPopup) {
    try { tg.showPopup({ title, message, buttons: [{ type: "close" }] }); return; } catch (_) { /* ниже */ }
  }
  alert(`${title}\n\n${message}`);
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of [].concat(children)) if (c) node.append(c);
  return node;
}

// Значок из спрайта в index.html.
function icon(name, size = 20, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  if (cls) svg.setAttribute("class", cls);
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// «#0B0F14» → «11, 15, 20»: плашка превью красится той же непрозрачностью, что
// в кадре, а значит цвет нужен разобранный.
function hexToRgb(hex) {
  let v = String(hex || "#000000").replace("#", "");
  if (v.length === 3) v = [...v].map((c) => c + c).join("");
  const n = parseInt(v, 16) || 0;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(", ");
}

function fmtSize(bytes) {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 ** 3).toFixed(1)} ${tr("size.gb")}`;
  return `${Math.max(1, Math.round(bytes / 1024 / 1024))} ${tr("size.mb")}`;
}
function fmtDuration(sec) {
  if (!isFinite(sec)) return "";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m ? `${m}:${String(s).padStart(2, "0")}` : `${s} ${tr("dur.s")}`;
}

function openTelegram(url) {
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, "_blank");
}

// Ссылка из настроек сервера: адрес Telegram открывает сам Telegram, остальное — браузер.
function openLink(url) {
  if (/^https:\/\/t\.me\//i.test(url)) openTelegram(url);
  else if (tg && tg.openLink) tg.openLink(url);
  else window.open(url, "_blank");
}

// ------------------------------------------------------------------ тема

const THEME_KEY = "reel-theme";
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  if (tg) {
    try { tg.setHeaderColor(bg); tg.setBackgroundColor(bg); tg.setBottomBarColor && tg.setBottomBarColor(bg); } catch (_) { /* старый клиент */ }
  }
  renderThemeSeg();
}
function setTheme(theme) {
  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* не критично */ }
  applyTheme(theme);
  haptic();
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) { /* приватный режим */ }
  // Тёмная — основа: приложение про видео, и так его рисует образец. Светлая —
  // если человек выбрал её сам или так настроен его Telegram.
  const initial = saved || (tg && tg.colorScheme === "light" ? "light" : "dark");
  applyTheme(initial);
  for (const btn of $$("[data-theme-toggle]")) {
    btn.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  }
}
function renderThemeSeg() {
  const box = $("#theme-seg");
  if (!box) return;
  const now = document.documentElement.dataset.theme;
  box.replaceChildren(...[["light", "sun"], ["dark", "moon"]].map(([key, ico]) =>
    el("button", {
      type: "button", class: key === now ? "is-active" : "", "aria-label": key,
      "aria-pressed": key === now ? "true" : "false",
      onclick: () => setTheme(key),
    }, [icon(ico, 16)])));
}

// ------------------------------------------------------------------ превью субтитров

// Шрифты превью объявляются по списку из /api/bootstrap: их несколько, и
// зашитый в css один означал бы, что чип выбора показывает чужой шрифт.
// Диапазон весов — тот же, что в рендере и в линейке замеров: один файл
// закрывает весь диапазон, и браузер не синтезирует жирность.
function declareFonts() {
  const faces = new Map();
  for (const p of state.boot.presets || []) {
    if (p.font_file && p.font) faces.set(p.font_file, p.font);
    // Второй шрифт темы — им набрано выделенное слово.
    if (p.accent_font) faces.set(p.accent_font.file, p.accent_font.family);
  }
  // На каждое семейство ДВА объявления с одним именем и разными наборами
  // символов. Браузер выбирает файл по тому, что реально на экране:
  //
  //   лента («Аа» и латинские названия тем) → урезанный файл, 8-19 КБ;
  //   превью (русская фраза)                → полный, и только для выбранного.
  //
  // Это снимает главную причину, по которой лента выглядела одинаковой:
  // восемнадцать полных файлов — 1406 КБ в gzip и 25 секунд по туннелю
  // (замер), и всё это время образцы рисуются одним системным шрифтом.
  // Восемнадцать урезанных весят 280 КБ на всех.
  //
  // Урезанный объявлен ВТОРЫМ: при пересечении наборов побеждает последний,
  // и буквы ленты берутся из маленького файла, а не из полного.
  const range = state.boot.font_ui_range;
  const css = [...faces].flatMap(([file, family]) => {
    const face = (src, extra) => `@font-face{font-family:"${family}";` +
      `src:url("${src}") format("truetype");font-weight:1 1000;font-display:swap;${extra}}`;
    const full = face(`/fonts/${file}`, "");
    return range ? [full, face(`/fonts/ui/${file}`, `unicode-range:${range};`)] : [full];
  }).join("\n");
  const tag = document.getElementById("font-faces") || el("style", { id: "font-faces" });
  tag.textContent = css;
  if (!tag.parentNode) document.head.appendChild(tag);

  // Просим файлы СРАЗУ, не дожидаясь, пока их потребует текст на экране.
  //
  // `@font-face` грузится лениво: пока лента выбора не нарисована, ни один
  // файл не запрошен. А рисуется она на шаге 2 — и первые секунды все
  // восемнадцать образцов показываются одним системным шрифтом, то есть
  // выглядят одинаково (замерено: ширина «Аа» у всех 25.2 px, через две
  // секунды — от 20.7 до 37.4). Здесь же загрузка начинается на первом
  // экране и идёт, пока человек выбирает видео.
  //
  if (!document.fonts) return;
  if (range) {
    // Просим ПОИМЕННО урезанные начертания, а не семейства.
    //
    // `document.fonts.load("21px \"X\"")` грузит ВСЕ подходящие начертания
    // семейства, а полное подходит к любому тексту — замерено: скачивались и
    // 17 урезанных, и 17 полных, то есть 3.2 МБ вместо 280 КБ, вдвое хуже,
    // чем было. Поэтому идём по самим объявлениям и будим только те, у
    // которых задан диапазон: полные браузер дотянет сам, когда превью
    // наберёт русскую фразу, и только для выбранного шрифта.
    for (const face of document.fonts) {
      if (face.unicodeRange && face.unicodeRange !== "U+0-10FFFF" && face.status === "unloaded") {
        face.load().catch(() => { /* нет файла — останется запасной */ });
      }
    }
    return;
  }
  // Сервер урезанных начертаний не отдаёт (старее приложения — так бывает,
  // когда статика уехала, а Python ещё нет). Тогда будим полные: ждать их
  // по-прежнему долго, но лента хотя бы не стоит одинаковой, пока человек
  // выбирает видео.
  //
  // Эта ветка не запасная вежливость, а починка: пока условие стояло на
  // `range`, при старом сервере не грузилось НИЧЕГО, и ранняя загрузка —
  // уже выложенная — в бою не работала.
  for (const family of new Set(faces.values())) {
    document.fonts.load(`21px "${family}"`).catch(() => { /* нет файла — останется запасной */ });
  }
}

function activePreset() {
  return (state.boot.presets || []).find((x) => x.name === state.preset) || null;
}

function activeThemeFamily() {
  const p = activePreset();
  return (p && p.font) || "Montserrat ExtraBold";
}


// Цвет выделения: выбранный, а иначе — задуманный темой.
function activeAccent() {
  const chosen = (state.boot.accents || []).find((a) => a.key === state.accent);
  const p = activePreset();
  return (chosen && chosen.color) || (p ? p.colors.accent : "#F3D28F");
}

const preview = {
  timer: null,
  paused: false,
  step: 0,
  script() {
    const title = (activePreset() || {}).title || "Glass";
    const [a1, a2, big1, a3, a4] = tr("preview.s1");
    const [b1, b2, big2] = tr("preview.s2");
    const [c1, c2, big3, c3] = tr("preview.s3");
    return [
      { small: [a1, a2], big: big1, after: [a3, a4, title] },
      { small: [b1, b2], big: big2, after: [] },
      { small: [c1, c2], big: big3, after: [c3] },
    ];
  },
  // Кегль превью — не вкус, а пересчёт: пиксели кадра 1920 в пиксели карточки.
  // Поэтому мелкая тема (Widescreen, 46 px) здесь и выглядит мелкой, а крупная
  // (Heavyweight, 109) занимает полкадра — как и будет в ролике.
  //
  size(p) {
    const k = ($("#preview").clientHeight || 620) / 1920;
    return { k, base: p.size * k };
  },
  place(box, p, k) {
    box.style.top = "auto";
    box.style.bottom = "auto";
    box.style.transform = "none";
    if (p.position === "bottom") box.style.bottom = `${Math.round(p.margin * k)}px`;
    else if (p.position === "top") box.style.top = `${Math.round(p.margin * k)}px`;
    else { box.style.top = "50%"; box.style.transform = "translateY(-50%)"; }
  },
  render() {
    const box = $("#preview-caption");
    const p = activePreset();
    if (!p) return;
    const accent = activeAccent();
    const tx = (w) => (p.uppercase ? w.toUpperCase() : w);
    const scenes = this.script();
    const scene = scenes[Math.floor(this.step / 10) % scenes.length];
    const shown = this.step % 10; // 0..9: слова загораются по одному, потом блок гаснет
    const { k, base } = this.size(p);

    box.replaceChildren();
    box.style.color = p.colors.text;
    box.style.fontSize = `${base}px`;
    box.style.lineHeight = p.line_height;
    box.style.letterSpacing = `${p.letter_spacing}em`;
    // Зазор между словами — число темы (пробел шрифта + разрядка + поправка),
    // то же, которым ролик раскладывает строку.
    if (typeof p.word_gap === "number") box.style.setProperty("--word-gap", `${p.word_gap}em`);
    box.style.setProperty("--cap-font", `"${activeThemeFamily()}"`);
    this.place(box, p, k);
    // Движение берётся у темы, а не зашито в стили: иначе превью обещает одно,
    // а рендер делает другое — ровно так и было, пока слово в ролике включалось
    // за кадр, а здесь плавно проявлялось.
    const motion = p.motion || {};
    box.style.setProperty("--cap-in", `${motion.word_in_ms || 0}ms`);
    box.style.setProperty("--cap-rise", `${motion.word_rise_em || 0}em`);

    let i = 0;
    const word = (w, size, color, family) => {
      const on = shown >= 1 && i < shown && shown < 9;
      i += 1;
      return el("span", {
        class: `cap-word${on ? " on" : ""}`,
        text: tx(w),
        style: `font-size:${size}px${color ? `;color:${color}` : ""}` +
          // `calt` выключен так же, как в ролике (Captions.tsx): превью
          // обязано показать те же формы букв, что будут в кадре.
          (family ? `;font-family:"${family}", system-ui, sans-serif;font-feature-settings:"calt" 0` : ""),
      });
    };
    // Второй шрифт темы — у выделенного слова.
    const pair = p.accent_font || null;
    const bigSize = base * (p.accent_scale || 1) * (pair ? pair.scale || 1 : 1);
    // Плашка — та же, что у блока в кадре: цвет, непрозрачность, радиус и
    // паддинги темы. У тем, где она приходит только над светлым фоном
    // (`opacity: 0`), в превью её и нет — фон карточки тёмный.
    const plate = el("div", { class: "cap-plate" });
    const panel = p.panel || {};
    let plateX = 0;
    if (panel.enabled && panel.opacity > 0) {
      plateX = Math.round(panel.padding_x * k);
      plate.style.background = `rgba(${hexToRgb(panel.color)}, ${panel.opacity})`;
      plate.style.padding = `${Math.round(panel.padding_y * k)}px ${plateX}px`;
      plate.style.borderRadius = `${Math.round(panel.radius * k)}px`;
    }
    plate.append(el("div", { class: "cap-line" }, scene.small.map((w) => word(w, base))));
    plate.append(el("div", { class: "cap-line" }, [word(scene.big, bigSize, accent, pair && pair.family)]));
    if (scene.after.length) {
      plate.append(el("div", { class: "cap-line" }, scene.after.map((w) => word(w, base))));
    }
    box.append(plate);

    // Строка ужимается под ширину превью, как в рендере (С1): слово «СУБТИТРОВ»
    // капсом в Bold иначе вылезает за края карточки.
    //
    // Паддинг плашки вычитается ЯВНО. Раньше тут стояло
    // `plate.offsetWidth - plate.clientWidth`, но clientWidth паддинг уже
    // включает: разность даёт рамку, а её у плашки нет — то есть вычиталось
    // всегда ноль. На единственной теме с видимой плашкой (Notebook) строка,
    // которая шире её внутренней коробки, проверку проходила и вылезала
    // словами за подложку.
    const room = box.clientWidth - 2 * plateX;
    for (const line of plate.children) {
      // Ширина строки — от левого края первого слова до правого края
      // последнего. `scrollWidth` у строки по центру считает только то, что
      // вылезло ВПРАВО: левая половина перебора терялась, строка ужималась
      // вдвое меньше нужного и первой буквой уходила за край кадра.
      const first = line.firstElementChild, last = line.lastElementChild;
      const width = first ? last.getBoundingClientRect().right - first.getBoundingClientRect().left : 0;
      const over = width / room;
      if (room > 0 && over > 1) {
        for (const w of line.children) {
          const size = parseFloat(getComputedStyle(w).fontSize);
          w.style.fontSize = `${Math.floor(size / over)}px`;
        }
      }
    }
    $("#preview-tag").textContent = p.label || p.title;
  },
  tick() {
    this.step += 1;
    this.render();
  },
  start() {
    clearInterval(this.timer);
    // При сокращённом движении кадр собирается один раз и стоит: мигание слов
    // раз в 280 мс — ровно то, от чего эта настройка защищает.
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      this.step = 5;
      this.render();
      return;
    }
    this.timer = setInterval(() => { if (!this.paused) this.tick(); }, 280);
    this.render();
  },
  stop() {
    clearInterval(this.timer);
    this.timer = null;
  },
  toggle() {
    this.paused = !this.paused;
    $("#preview").classList.toggle("is-paused", this.paused);
    $("#preview-play").setAttribute("aria-label", tr(this.paused ? "preview.play" : "preview.pause"));
  },
};

// Превью живёт только на втором шаге монтажа. У спрятанного `display:none`
// высота равна нулю, масштаб уходил на запасные 620, и на возврате кадр на
// четверть секунды показывал субтитр в полтора раза крупнее, с неужатыми
// строками. Заодно это 3.6 перерисовки в секунду, которые шли бы вечно.
function syncPreview() {
  if (state.screen === "edit" && state.step === 2 && !document.hidden) preview.start();
  else preview.stop();
}

// ------------------------------------------------------------------ экран «Монтаж», шаг 2

// Лента перерисовывается целиком, поэтому к активному элементу прокручиваемся
// уже ПОСЛЕ замены: старый узел к этому моменту не в документе.
//
// Двигаем САМУ ленту, а не зовём scrollIntoView: тот бы потянул и страницу по
// вертикали, а на открытии приложения это прыжок экрана ещё до первого
// касания.
// Вызывается ОДИН раз — когда шаг оформления открывается. Позже лента сама не
// ездит никогда: человек уже листает её и целится в плитку, а самоходная лента
// уводит плитку из-под пальца.
function scrollToActive(box) {
  const active = box.querySelector(".is-active");
  if (!active || box.scrollWidth <= box.clientWidth) return;
  const a = active.getBoundingClientRect();
  const b = box.getBoundingClientRect();
  jumpTo(box, box.scrollLeft + a.left - b.left - (b.width - a.width) / 2);
  // Дальше держим на месте именно выбранную плитку: шрифты ещё едут.
  pinAnchor(box, [...box.children].indexOf(active));
}

// Поставить ленту в точку мгновенно. У ленты в CSS плавная прокрутка, и обычное
// присваивание `scrollLeft` превращается в езду; `behavior: "instant"` знают
// не все клиенты. Встроенный стиль выключает плавность наверняка и везде.
function jumpTo(box, left) {
  const was = box.style.scrollBehavior;
  box.style.scrollBehavior = "auto";
  box.scrollLeft = left;
  box.style.scrollBehavior = was;
}

// ЯКОРЬ ЛЕНТЫ. Подписи плиток набраны шрифтами тем, а шрифты приезжают не
// сразу — по туннелю секундами. Каждый приехавший шрифт меняет ширину своей
// плитки, и всё, что правее, сдвигается: человек нажал на стиль, а лента ещё
// несколько секунд ползёт под пальцем (замер с медленными шрифтами: 49 px за
// шесть секунд). Поэтому у ленты есть якорь — плитка, которая обязана стоять
// на месте, — и на каждое изменение ширины плиток прокрутка поправляется так,
// чтобы якорь не сдвинулся. Поправка идёт из ResizeObserver, то есть до
// отрисовки кадра: сдвига на экране нет вовсе.
//
// Якорь — плитка, на которую нажали (или выбранная при открытии шага), пока
// она видна; ушла из вида — самая левая видимая.
const stripAnchors = new WeakMap();

function anchorOf(box, pinned) {
  const b = box.getBoundingClientRect();
  const kids = box.children;
  const at = (i) => {
    const r = kids[i].getBoundingClientRect();
    return r.right > b.left + 1 && r.left < b.right - 1 ? { index: i, x: r.left - b.left, pinned } : null;
  };
  if (pinned !== null && pinned >= 0 && pinned < kids.length) {
    const held = at(pinned);
    if (held) return held;
  }
  for (let i = 0; i < kids.length; i++) {
    const seen = at(i);
    if (seen) return { ...seen, pinned: null };
  }
  return null;
}

function pinAnchor(box, index) {
  const anchor = anchorOf(box, index);
  if (anchor) stripAnchors.set(box, anchor);
  else stripAnchors.delete(box);
}

// Лента прокручена пальцем или стрелкой: якорь тот же, место — новое.
//
// Собственную поправку (`holdAnchor`) сюда не пускаем: прокрутка округляется
// до долей пикселя, и перезапись места после каждой поправки копила бы эту
// ошибку — десять приехавших шрифтов сдвигали плитку на шесть пикселей.
function saveAnchor(box) {
  const was = stripAnchors.get(box);
  if (was && was.heldAt === box.scrollLeft) return;
  pinAnchor(box, was ? was.pinned : null);
}

// Плитки сменили ширину: вернуть якорь туда, где он стоял.
function holdAnchor(box) {
  const anchor = stripAnchors.get(box);
  const kid = anchor && box.children[anchor.index];
  if (!kid) return;
  const drift = kid.getBoundingClientRect().left - box.getBoundingClientRect().left - anchor.x;
  if (Math.abs(drift) < 0.5) return;
  jumpTo(box, box.scrollLeft + drift);
  anchor.heldAt = box.scrollLeft;
}

const pillWatch = new WeakMap();
function watchPills(box) {
  if (!window.ResizeObserver) return;
  let watch = pillWatch.get(box);
  if (!watch) {
    watch = new ResizeObserver(() => holdAnchor(box));
    pillWatch.set(box, watch);
  }
  watch.disconnect();
  for (const kid of box.children) watch.observe(kid);
}

// Стрелки ленты видны, только когда в их сторону есть куда листать: стрелка
// «назад» на самом начале ленты обещала бы то, чего нет. Пересчитываются на
// прокрутке, на смене размера и после каждой перерисовки ленты — ширина
// таблеток меняется с языком и с загрузкой шрифтов тем.
function syncArrows(strip) {
  const track = strip.querySelector(".strip__track");
  const max = track.scrollWidth - track.clientWidth;
  const prev = max > 2 && track.scrollLeft > 2;
  const next = max > 2 && track.scrollLeft < max - 2;
  strip.querySelector(".strip__arrow--prev").hidden = !prev;
  strip.querySelector(".strip__arrow--next").hidden = !next;
  strip.classList.toggle("has-prev", prev);
  strip.classList.toggle("has-next", next);
}
function syncAllArrows() {
  for (const strip of $$(".strip")) syncArrows(strip);
}
function initStrips() {
  for (const strip of $$(".strip")) {
    const track = strip.querySelector(".strip__track");
    track.addEventListener("scroll", () => { syncArrows(strip); saveAnchor(track); }, { passive: true });
    for (const btn of strip.querySelectorAll(".strip__arrow")) {
      btn.addEventListener("click", () => {
        haptic();
        const step = Math.max(160, track.clientWidth * 0.7) * Number(btn.dataset.dir);
        try { track.scrollBy({ left: step, behavior: "smooth" }); } catch (_) { track.scrollLeft += step; }
      });
    }
    if (window.ResizeObserver) new ResizeObserver(() => syncArrows(strip)).observe(track);
  }
}

// Подпись таблетки. У темы с парой шрифтов последнее слово подписи набрано
// вторым шрифтом — пара видна ещё в ленте, до нажатия.
function pillName(p) {
  const label = p.uppercase ? (p.label || p.title).toUpperCase() : (p.label || p.title);
  const attrs = {
    class: `style-pill__name${p.uppercase ? " is-caps" : ""}`,
    style: `font-family:"${p.font}", system-ui, sans-serif;letter-spacing:${p.letter_spacing}em`,
  };
  const cut = label.lastIndexOf(" ");
  if (!p.accent_font || cut < 0) return el("span", { ...attrs, text: label });
  return el("span", attrs, [
    el("span", { text: label.slice(0, cut + 1) }),
    el("span", {
      class: "style-pill__accent",
      text: label.slice(cut + 1),
      style: `font-family:"${p.accent_font.family}", system-ui, sans-serif`,
    }),
  ]);
}

// Перерисовать ленту, не сдвинув её: после выбора лента обязана стоять там,
// где её оставил палец. Раньше она сама ехала, ставя выбранное по центру, —
// человек тянулся к соседней таблетке, а та уезжала из-под пальца.
// `scrollLeft` присваивается напрямую и мгновенно: у ленты в CSS плавная
// прокрутка, и обычное присваивание превратилось бы в ту же езду.
function keepScroll(box, redraw) {
  const left = box.scrollLeft;
  redraw();
  if (box.scrollLeft !== left) jumpTo(box, left);
  // Плитки новые — следим за шириной уже их. Якорь — выбранная плитка: на неё
  // только что нажали, и уехать из-под пальца она не должна.
  pinAnchor(box, [...box.children].findIndex((kid) => kid.classList.contains("is-active")));
  watchPills(box);
}

function renderPresets() {
  const box = $("#preset-tabs");
  keepScroll(box, () => box.replaceChildren(...state.boot.presets.map((p) =>
    el("button", {
      class: `style-pill${p.name === state.preset ? " is-active" : ""}`,
      type: "button",
      // Не «вкладка»: панели у неё нет, и скринридер обещал бы то, чего нет.
      // Это выбор одного из многих — радиогруппа.
      role: "radio",
      title: `${p.label || p.title} · ${p.title}`,
      "aria-checked": p.name === state.preset ? "true" : "false",
      onclick: () => {
        state.preset = p.name;
        haptic();
        renderPresets();
        renderAccents();
        preview.render();
      },
    }, [
      // Таблетка — это образец: подпись набрана шрифтом темы и её регистром.
      // Семнадцать одинаково набранных подписей ничего не говорили бы о том,
      // чем темы отличаются. Текст — понятная подпись («Крупные субтитры»), а
      // не название темы: «Chisel» тому, кто видит её впервые, не говорит ничего.
      pillName(p),
    ]))));
  syncArrows(box.parentNode);
}

function renderAccents() {
  const p = activePreset();
  const box = $("#accent-chips");
  keepScroll(box, () => box.replaceChildren(...state.boot.accents.map((a) =>
    el("button", {
      class: `swatch${a.key === state.accent ? " is-active" : ""}`,
      type: "button",
      role: "radio",
      "aria-checked": a.key === state.accent ? "true" : "false",
      title: a.title,
      onclick: () => {
        state.accent = a.key;
        haptic();
        renderAccents();
        preview.render();
      },
    }, [
      el("i", { class: "swatch__dot", style: `background:${a.color || (p ? p.colors.accent : "#F3D28F")}` }),
      el("span", { class: "swatch__name", text: a.title }),
    ]))));
  syncArrows($("#accent-chips").parentNode);
}

function renderOptions() {
  for (const input of $$("[data-option]")) input.checked = !!state.options[input.dataset.option];
  renderCut();
  renderLayout();
}

// Какие переключатели видны. Сервер прислал те, что у него что-то меняют
// (`boot.switches`): галочка, которая ничего не меняет, хуже отсутствующей. А в
// «Двух видео» нет картинок по смыслу, графики, переходов и полосы прогресса —
// верхнюю половину человек показывает сам (`data-single-only` в разметке).
// Карточка, в которой не осталось ни одной строки, прячется целиком.
function renderSwitches() {
  const split = state.layout === "split";
  const live = state.boot && state.boot.switches;
  for (const card of $$(".card.options")) {
    let shown = 0;
    for (const row of card.querySelectorAll(".toggle-row")) {
      const key = row.querySelector("[data-option]").dataset.option;
      row.hidden = (!!live && !live.includes(key)) || (split && row.hasAttribute("data-single-only"));
      if (!row.hidden) shown += 1;
    }
    card.hidden = shown === 0;
  }
}

// Раскладка кадра — выбор из двух, как нарезка. Помнится между заходами: кто
// снимает ролики «сверху товар, снизу я», снимает их подряд.
const LAYOUT_KEY = "reelbot:layout";

function loadLayout() {
  try { return localStorage.getItem(LAYOUT_KEY) === "split" ? "split" : "single"; } catch (_) { return "single"; }
}

function renderLayout() {
  const split = state.layout === "split";
  $("#layout-seg").replaceChildren(...[["single", "layout.single"], ["split", "layout.split"]].map(([value, key]) =>
    el("button", {
      type: "button", role: "radio", class: value === state.layout ? "is-active" : "",
      "aria-checked": value === state.layout ? "true" : "false", text: tr(key),
      onclick: () => {
        if (state.busy || state.layout === value) return;
        state.layout = value;
        try { localStorage.setItem(LAYOUT_KEY, value); } catch (_) { /* не критично */ }
        haptic();
        renderLayout();
      },
    })));
  $("#split-frame").classList.toggle("is-split", split);
  $("#top-dropzone").hidden = !split;
  $("#drop-empty-title").textContent = tr(split ? "split.bottom_title" : "cta.choose");
  $("#drop-role").textContent = tr("split.bottom_sub");
  $("#drop-role").hidden = !split;
  $("#drop-role-chip").hidden = !split;
  $("#s1-lead").textContent = tr(split ? "edit.s1_lead_split" : (maxClips() > 1 ? "edit.s1_lead" : "edit.s1_lead_one"));
  // Два файла в чат с ботом не отправить.
  $("#send-in-chat").hidden = split;
  renderSwitches();
  // В «Двух видео» снизу одно видео: от списка остаётся первое.
  if (split && state.clips.length > 1) state.clips.length = 1;
  renderClips();
  renderNext();
  renderCta();
}

// Готово ли к монтажу: в «Двух видео» нужны оба файла.
function ready() {
  return !!state.file && (state.layout !== "split" || !!state.topFile);
}

// Какое поле открыть, если файла не хватает.
function missingInput() {
  return $(!state.file ? "#file-input" : "#top-input");
}

// ------------------------------------------------------------------ экран «Монтаж», шаг 1

// Нарезка — выбор из двух, а не тумблер: на образце это режимы, и «без
// нарезки» читается как осознанный выбор, а не забытая галочка. Третьего
// режима нет, пока пайплайн его не умеет (`core.options`).
function renderCut() {
  const box = $("#cut-seg");
  const on = !!state.options.cut;
  box.replaceChildren(...[[true, "cut.on"], [false, "cut.off"]].map(([value, key]) =>
    el("button", {
      type: "button", role: "radio", class: value === on ? "is-active" : "",
      "aria-checked": value === on ? "true" : "false", text: tr(key),
      onclick: () => { state.options.cut = value; haptic(); renderCut(); },
    })));
  $("#cut-hint").textContent = tr(on ? "cut.on_hint" : "cut.off_hint");
}

function renderLimits() {
  const l = state.boot.limits;
  // Под «Выбрать видео» — что видео можно несколько; сами пределы длины и
  // размера живут в «Требованиях к видео». Сервер склейку не умеет — пределы.
  $("#drop-limits").textContent = maxClips() > 1
    ? tr("edit.drop_many")
    : tr("edit.limits", { mb: l.max_upload_mb, min: l.min_seconds, max: l.max_minutes });
}

// Поля загрузки: нижнее (говорящий, оно же единственное в обычном режиме) и
// верхнее режима «Два видео».
const SLOTS = {
  bottom: { key: "file", zone: "#dropzone", thumb: "#drop-thumb", title: "#drop-title", sub: "#drop-sub", input: "#file-input", remove: "#drop-remove" },
  top: { key: "topFile", zone: "#top-dropzone", thumb: "#top-thumb", title: "#top-title", sub: "#top-sub", input: "#top-input", remove: "#top-remove" },
};

// Сколько видео склеивается в один ролик. Число даёт сервер (`pipeline.join`).
function maxClips() {
  return (state.boot && state.boot.limits.max_clips) || 1;
}

function clipOf(file) {
  return state.clips.find((c) => c.file === file);
}

// Длина ролика считается по всем кускам вместе: сервер проверяет её так же.
function warnTotalLength() {
  const lim = state.boot.limits;
  if (!state.clips.length || state.clips.some((c) => !isFinite(c.duration))) return;
  const total = state.clips.reduce((n, c) => n + c.duration, 0);
  if (total > lim.max_minutes * 60 + 1) toast(tr("file.too_long", { max: lim.max_minutes }));
  else if (state.clips.length === 1 && total < lim.min_seconds) toast(tr("file.too_short", { min: lim.min_seconds }));
}

// Добавить видео в ролик. В «Двух видео» кусок один — новый заменяет прежний.
function addClips(files) {
  if (!files.length) return;
  if (state.layout === "split") { setFile(files[0]); return; }
  const first = !state.clips.length;
  for (const file of files) {
    if (state.clips.length >= maxClips()) { toast(tr("clips.max", { n: maxClips() })); break; }
    const clip = { file, duration: NaN, thumb: "" };
    state.clips.push(clip);
    // Первый кусок осматривает `setFile` — он же держит поле «Двух видео».
    if (state.clips[0] === clip) continue;
    inspectVideo(file, () => state.clips.includes(clip), (d) => {
      clip.duration = d;
      renderClips();
      warnTotalLength();
    }, (dataUrl) => { clip.thumb = dataUrl; renderClips(); });
  }
  if (first) setFile(state.clips[0].file);
  else { renderClips(); renderCta(); }
}

function removeClip(index) {
  state.clips.splice(index, 1);
  const head = state.clips[0] ? state.clips[0].file : null;
  // Убрали первый — его место занимает следующий, и поле «Двух видео» должно
  // показывать уже его.
  if (state.file !== head) setFile(head);
  else { renderClips(); renderCta(); }
}

// Список видео показан вместо поля выбора, как только выбрано хотя бы одно. В
// «Двух видео» списка нет: там два поля в рамке кадра.
function renderClips() {
  const show = state.layout !== "split" && state.clips.length > 0;
  $("#clips").hidden = !show;
  $("#dropzone").hidden = show;
  if (!show) return;
  const n = state.clips.length;
  $("#clips-title").textContent = tr("clips.title", { n });
  const known = state.clips.every((c) => isFinite(c.duration));
  const total = state.clips.reduce((sum, c) => sum + (c.duration || 0), 0);
  $("#clips-total").textContent = known ? tr("clips.total", { t: fmtDuration(total) }) : "";
  $("#clips-list").replaceChildren(...state.clips.map((clip, i) => {
    const thumb = el("span", { class: `clip__thumb${clip.thumb ? " has-image" : ""}` }, [
      icon("film", 20), el("i", { class: "clip__no", text: String(i + 1) }),
    ]);
    if (clip.thumb) thumb.style.backgroundImage = `url(${clip.thumb})`;
    const meta = [isFinite(clip.duration) ? fmtDuration(clip.duration) : "", fmtSize(clip.file.size)].filter(Boolean).join(" · ");
    return el("div", { class: "clip" }, [
      thumb,
      el("span", { class: "clip__text" }, [
        el("b", { text: clip.file.name || tr("edit.untitled") }),
        el("span", { class: "muted", text: meta }),
      ]),
      el("button", {
        class: "clip__remove", type: "button", "aria-label": tr("edit.remove"),
        onclick: () => { if (!state.busy) { removeClip(i); haptic(); } },
      }, [icon("close", 18)]),
    ]);
  }));
  $("#clips-add").hidden = n >= maxClips();
}

// Длительность читаем браузером до загрузки: слишком длинное видео лучше
// отбить сразу, а не после минуты заливки по мобильной сети. Заодно снимаем
// кадр на обложку: имя файла вроде `IMG_4821.mov` не говорит, то ли это видео.
// `alive()` — файл всё ещё выбран; `onMeta(секунды)`, `onThumb(dataURL)`.
function inspectVideo(file, alive, onMeta, onThumb) {
  const url = URL.createObjectURL(file);
  const probe = document.createElement("video");
  probe.preload = "metadata";
  probe.muted = true;
  probe.playsInline = true;
  // Освобождение — один раз и в любом случае. Перемотка может завершиться
  // вообще без события (`seekable` пуст — так бывает у .mkv и .webm без
  // индекса): тогда ни `seeked`, ни `error` не придут, и видео с буфером
  // осталось бы жить до перезагрузки страницы. Сторож снимает это через три
  // секунды — обложки не будет, остальное работает.
  let freed = false;
  const done = () => {
    if (freed) return;
    freed = true;
    clearTimeout(guard);
    probe.removeAttribute("src");
    URL.revokeObjectURL(url);
  };
  const guard = setTimeout(done, 3000);
  probe.onloadedmetadata = () => {
    const d = probe.duration;
    if (!alive()) { done(); return; }
    onMeta(d);
    try { probe.currentTime = Math.min(0.5, (isFinite(d) ? d : 1) / 2); } catch (_) { done(); }
  };
  probe.onseeked = () => {
    try {
      const side = Math.min(probe.videoWidth, probe.videoHeight);
      if (side && alive()) {
        const c = document.createElement("canvas");
        c.width = c.height = 128;
        c.getContext("2d").drawImage(
          probe, (probe.videoWidth - side) / 2, (probe.videoHeight - side) / 2, side, side, 0, 0, 128, 128,
        );
        onThumb(c.toDataURL("image/jpeg", 0.72));
      }
    } catch (_) { /* кодек не отдаёт кадр в canvas — остаётся иконка */ }
    done();
  };
  probe.onerror = done;
  probe.src = url;
}

function setFile(file, which = "bottom") {
  const slot = SLOTS[which];
  state[slot.key] = file;
  // Нижнее поле и первый кусок ролика — один и тот же файл: выбор в поле
  // заводит список заново, а сброс поля его очищает.
  if (which === "bottom") {
    if (!file) state.clips = [];
    else if (!state.clips.length || state.clips[0].file !== file) state.clips = [{ file, duration: NaN, thumb: "" }];
  }
  const zone = $(slot.zone);
  const thumb = $(slot.thumb);
  zone.classList.toggle("has-file", !!file);
  thumb.style.backgroundImage = "";
  thumb.classList.remove("has-image");
  renderNext();
  renderClips();
  if (!file) {
    $(slot.input).value = "";
    renderCta();
    return;
  }
  $(slot.title).textContent = file.name || tr("edit.untitled");
  $(slot.sub).textContent = fmtSize(file.size);
  inspectVideo(file, () => state[slot.key] === file, (d) => {
    $(slot.sub).textContent = `${fmtSize(file.size)} · ${fmtDuration(d)}`;
    // Пределы длины — у речи. Верхнему они не нужны: короткое просто раньше
    // раскроет нижнее на весь экран, длинное обрежется.
    if (which !== "bottom") return;
    const clip = clipOf(file);
    if (clip) clip.duration = d;
    renderClips();
    warnTotalLength();
  }, (dataUrl) => {
    thumb.style.backgroundImage = `url(${dataUrl})`;
    thumb.classList.add("has-image");
    const clip = which === "bottom" ? clipOf(file) : null;
    if (clip) { clip.thumb = dataUrl; renderClips(); }
  });
  renderCta();
}

function renderNext() {
  const btn = $("#next");
  btn.classList.toggle("is-idle", !ready());
  $("#next-sub").textContent = tr(
    ready() ? "edit.next_ready" : (state.layout === "split" && (state.file || state.topFile) ? "split.need_both" : "edit.next_idle"),
  );
}

function setStep(step) {
  state.step = step;
  for (const s of $$("[data-step]")) s.hidden = Number(s.dataset.step) !== step;
  syncDesignMode();
  window.scrollTo({ top: 0 });
  renderHeader();
  syncBackButton();
  syncPreview();
  if (step === 2) {
    // Ленты меряются только видимыми: на скрытом шаге ширина нулевая. А шрифты
    // плиток браузер начинает тянуть, только когда шаг стал видимым, — первый
    // замер идёт по подставному шрифту, поэтому второй ждёт их загрузки.
    //
    // Ленты ставятся к выбранному ОДИН раз, сейчас. Когда шрифты приедут,
    // стрелки и превью пересчитываются, а ленты не трогаются: человек к этому
    // времени уже листает и нажимает, и лента, уехавшая к выбранному стилю,
    // уводит плитку из-под пальца. Ширину плиток, которая меняется с каждым
    // приехавшим шрифтом, держит якорь ленты (`holdAnchor`).
    const refit = () => {
      syncAllArrows();
      preview.render();
    };
    for (const id of ["#preset-tabs", "#accent-chips"]) scrollToActive($(id));
    refit();
    if (document.fonts && document.fonts.ready) requestAnimationFrame(() => document.fonts.ready.then(refit));
  }
}

// Шаг оформления на телефоне занимает ровно окно: превью сверху, под ним
// вкладки, и листается только открытая вкладка. Класс на <html>, потому что
// подстраивать надо и отступ под нижней панелью у .main.
function syncDesignMode() {
  document.documentElement.classList.toggle("is-design", state.screen === "edit" && state.step === 2);
}

// Высота нижней панели нужна вёрстке шага оформления: окно минус шапка минус
// панель. Меряем, а не угадываем: у неё отступ под «чёлку», а с 720 px она
// скрыта вовсе.
function syncTabbarHeight() {
  const bar = $(".tabbar");
  if (!bar) return;
  const put = () => document.documentElement.style.setProperty("--tabbar-h", `${bar.offsetHeight}px`);
  put();
  if (window.ResizeObserver) new ResizeObserver(put).observe(bar);
  addEventListener("resize", put, { passive: true });
}

function initEdit() {
  $("#preview-play").addEventListener("click", () => { preview.toggle(); haptic(); });
  $("#requirements").addEventListener("click", () => {
    const l = state.boot.limits;
    popup(tr("req.title"), [
      tr("req.duration", { min: l.min_seconds, max: l.max_minutes }),
      tr("req.size", { mb: l.max_upload_mb }),
      tr("req.speaker"),
      tr("req.orientation"),
      maxClips() > 1 ? tr("req.clips", { n: maxClips() }) : "",
    ].filter(Boolean).join("\n"));
  });
  $("#send-in-chat").addEventListener("click", () => {
    haptic();
    openTelegram(`https://t.me/${state.boot.bot_username}`);
  });

  for (const [which, slot] of Object.entries(SLOTS)) {
    const input = $(slot.input);
    input.addEventListener("change", () => {
      const files = Array.from(input.files || []);
      if (which !== "bottom") { setFile(files[0] || null, which); return; }
      // Нижнее поле принимает несколько видео разом и добавляет к уже
      // выбранным. Отмена выбора ничего не сбрасывает: убирают видео крестиком.
      // Значение чистим, чтобы тот же файл можно было выбрать снова.
      if (files.length && !state.busy) addClips(files);
      input.value = "";
    });
    const zone = $(slot.zone);
    zone.addEventListener("click", (ev) => {
      if (state[slot.key] && ev.target.closest(slot.remove)) {
        ev.preventDefault();
        if (!state.busy) { setFile(null, which); haptic(); }
      } else if (state.busy) {
        ev.preventDefault();
      }
    });
    zone.addEventListener("dragover", (ev) => { ev.preventDefault(); zone.classList.add("is-over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
    zone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      zone.classList.remove("is-over");
      const dropped = Array.from(ev.dataTransfer.files || []);
      if (!dropped.length || state.busy) return;
      if (which === "bottom") addClips(dropped);
      else setFile(dropped[0], which);
    });
  }

  // «Добавить ещё видео»: тот же выбор файлов, что у поля, плюс перетаскивание.
  const add = $("#clips-add");
  add.addEventListener("click", () => { if (!state.busy) { haptic(); $("#file-input").click(); } });
  add.addEventListener("dragover", (ev) => { ev.preventDefault(); add.classList.add("is-over"); });
  add.addEventListener("dragleave", () => add.classList.remove("is-over"));
  add.addEventListener("drop", (ev) => {
    ev.preventDefault();
    add.classList.remove("is-over");
    const dropped = Array.from(ev.dataTransfer.files || []);
    if (dropped.length && !state.busy) addClips(dropped);
  });

  for (const box of $$("[data-option]")) {
    box.addEventListener("change", () => { state.options[box.dataset.option] = box.checked; haptic(); });
  }
  for (const btn of $$("[data-help]")) {
    btn.addEventListener("click", () => popup(...tr(`help.${btn.dataset.help}`)));
  }
  $("#next").addEventListener("click", () => {
    if (!ready()) { haptic("warning"); missingInput().click(); return; }
    haptic();
    setStep(2);
  });
  $("#to-step1").addEventListener("click", () => { haptic(); setStep(1); });
  $("#cta").addEventListener("click", onCta);
  initMusic();
}

// ------------------------------------------------------------------ музыка из рилса

function mmss(seconds) {
  const s = Math.round(seconds || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function renderMusic() {
  const m = state.music;
  const card = $("#music-card");
  if (!card) return;
  card.dataset.state = m.status;
  $("#music-clear").hidden = m.status === "empty" && !$("#music-input").value;
  $("#music-paste").hidden = !$("#music-clear").hidden;
  const text = {
    empty: tr("music.hint"),
    checking: tr("music.checking"),
    ready: tr("music.ready", { t: mmss(m.duration) }),
    error: m.error || "",
  }[m.status];
  $("#music-status-text").textContent = text;
}

function clearMusic() {
  state.music = { status: "empty", id: null, duration: null, error: null, seq: state.music.seq + 1, url: "" };
  $("#music-input").value = "";
  renderMusic();
}

async function checkMusic(raw) {
  const url = (raw || "").trim();
  if (url === state.music.url && state.music.status !== "error") return;
  const seq = state.music.seq + 1;
  if (!url) { clearMusic(); return; }
  state.music = { status: "checking", id: null, duration: null, error: null, seq, url };
  renderMusic();
  try {
    const res = await api("/api/music", { method: "POST", body: { url } });
    if (state.music.seq !== seq) return;
    state.music = { ...state.music, status: "ready", id: res.music_id, duration: res.duration };
    haptic("success");
  } catch (e) {
    if (state.music.seq !== seq) return;
    state.music = { ...state.music, status: "error", error: e.message };
    haptic("error");
  }
  renderMusic();
}

function initMusic() {
  const input = $("#music-input");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    renderMusic();
    timer = setTimeout(() => checkMusic(input.value), 600);
  });
  input.addEventListener("paste", () => {
    clearTimeout(timer);
    setTimeout(() => checkMusic(input.value), 0);
  });
  input.addEventListener("change", () => { clearTimeout(timer); checkMusic(input.value); });
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") input.blur(); });
  $("#music-paste").addEventListener("click", async () => {
    haptic();
    try {
      const text = await navigator.clipboard.readText();
      if (text) { input.value = text.trim(); checkMusic(input.value); return; }
    } catch (_) { /* Telegram WebView часто не даёт буфер — тогда просто поле */ }
    input.focus();
  });
  $("#music-clear").addEventListener("click", () => { haptic(); clearMusic(); });
  renderMusic();
}

// Платит ли пользователь за ролик. Владельцы не платят никогда, остальные —
// пока не включён бесплатный режим (`free` в bootstrap, FREE_VIDEOS на сервере).
function pays(u) {
  return !u.is_admin && !(state.boot && state.boot.free);
}

function renderCta(progressText, share) {
  const btn = $("#cta");
  const u = state.boot && state.boot.user;
  const title = $("#cta-title");
  const sub = $("#cta-sub");
  $("#cta-fill").style.width = share ? `${Math.round(share * 100)}%` : "0";
  if (!u) return;
  if (progressText) {
    title.textContent = progressText;
    sub.textContent = tr("cta.keep_open");
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  if (pays(u) && u.balance < videoCost()) {
    title.textContent = tr("cta.topup");
    sub.textContent = tr("cta.missing", { n: tokensText(videoCost() - u.balance) });
  } else {
    // Пока файла нет, нажатие открывает выбор файла, а не запускает монтаж —
    // так кнопка и должна называться.
    title.textContent = tr(ready() ? "cta.edit" : "cta.choose");
    // Владельцу — что монтаж ему ничего не стоит; остальным в бесплатном режиме —
    // нейтральная подсказка, без «бесплатно»: продукт выглядит готовым к продаже.
    const free = u.is_admin ? "cta.free" : "cta.ready";
    sub.textContent = !ready()
      ? tr(state.layout === "split" ? "split.need_both" : "cta.pick")
      : (pays(u) ? tr("cta.charge", { c: tokensText(videoCost()), n: u.balance }) : tr(free));
  }
}

async function onCta() {
  if (state.busy) return;
  const u = state.boot.user;
  if (pays(u) && u.balance < videoCost()) { go("tariffs"); return; }
  if (!ready()) { haptic("warning"); missingInput().click(); return; }

  state.busy = true;
  try {
    // Ссылка ещё проверяется — ролик уйдёт без музыки, и сказать об этом надо
    // сейчас: сервер про незаконченную проверку не знает.
    const musicPending = state.music.status === "checking";
    const res = await uploadAndStart();
    const job = res.job;
    haptic("success");
    setFile(null);
    setFile(null, "top");
    clearMusic();
    state.step = 1;
    for (const s of $$("[data-step]")) s.hidden = Number(s.dataset.step) !== 1;
    await refreshBoot();
    toast(res.music_dropped || musicPending ? tr("music.dropped") : tr("toast.accepted"));
    state.jobs = [job, ...state.jobs.filter((j) => j.id !== job.id)];
    state.jobsLoaded = true;
    go("videos", { replace: true });
  } catch (e) {
    haptic("error");
    if (e.status === 402) { await refreshBoot(); go("tariffs"); }
    popup(tr("err.title"), e.message);
  } finally {
    state.busy = false;
    renderCta();
  }
}

// Один файл кусками; `onBytes(сколько принято)` — для общей полосы прогресса.
async function uploadOne(file, onBytes) {
  const start = await api("/api/uploads", { method: "POST", body: { size: file.size, name: file.name } });
  const id = start.upload_id;
  const size = file.size;
  let offset = start.received || 0;
  let failures = 0;
  while (offset < size) {
    const end = Math.min(size, offset + start.chunk_bytes);
    onBytes(offset);
    try {
      const res = await api(`/api/uploads/${id}?offset=${offset}`, { method: "PUT", body: file.slice(offset, end), raw: true });
      offset = res.received;
      failures = 0;
    } catch (e) {
      // 409 — сервер принял больше или меньше, чем мы думаем: продолжаем с его числа.
      if (e.status === 409 && e.data && typeof e.data.received === "number") { offset = e.data.received; continue; }
      if (e.status && e.status < 500) throw e;
      failures += 1;
      if (failures > 5) throw new Error(tr("upload.lost"));
      await new Promise((r) => setTimeout(r, 1500 * failures));
    }
  }
  return id;
}

// Нижнее видео, затем верхнее («Два видео»), и одна задача на оба. Полоса —
// общая по байтам: два прогресса подряд читались бы как «загрузилось и
// началось заново».
async function uploadAndStart() {
  const split = state.layout === "split";
  const files = split ? [state.file, state.topFile] : state.clips.map((c) => c.file);
  const total = files.reduce((n, f) => n + f.size, 0) || 1;
  let before = 0;
  const show = (sent) => {
    const share = (before + sent) / total;
    renderCta(tr("cta.uploading", { p: Math.floor(share * 100) }), share);
  };
  const ids = [];
  for (const f of files) {
    ids.push(await uploadOne(f, show));
    before += f.size;
  }
  renderCta(tr("cta.checking"), 1);
  const body = {
    preset: state.preset,
    // Шрифта в опциях нет: его задаёт стиль (отдельный выбор убран).
    options: { ...state.options, accent: state.accent },
  };
  // Второй файл — верх «Двух видео», а в обычном режиме остальные файлы —
  // продолжение ролика: сервер склеит их по порядку в один исходник.
  if (split) { if (ids[1]) body.top_upload_id = ids[1]; }
  else if (ids.length > 1) body.extra_upload_ids = ids.slice(1);
  // Трек уходит, только если уже проверен. Пока ссылка проверяется, ролик
  // собирается без музыки — ждать её кнопка не обязана.
  if (state.music.status === "ready" && state.music.id) body.music_id = state.music.id;
  return api(`/api/uploads/${ids[0]}/finish`, { method: "POST", body });
}

// ------------------------------------------------------------------ «Главная»

function jobTitle() {
  return tr("jobs.reel");
}

const isActive = (j) => j.status === "queued" || j.status === "running";

function fmtClock(iso) {
  return iso ? new Date(iso).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" }) : "";
}
function fmtDay(iso) {
  return iso ? new Date(iso).toLocaleDateString(locale(), { day: "numeric", month: "long" }) : "";
}

// Вторая строка ролика: «00:38 · хранится до 24 дек.».
function jobSub(j) {
  if (isActive(j)) return `${tr(`status.${j.status}`)} · ${Math.round((j.progress || 0) * 100)}%`;
  const parts = [fmtClock(j.created_at)];
  if (j.status !== "done") parts.push(tr(`status.${j.status}`));
  else if (j.purged) parts.push(tr("jobs.purged_short"));
  else if (j.expires_at) parts.push(tr("jobs.keep", { d: fmtDate(j.expires_at) }));
  return parts.filter(Boolean).join(" · ");
}

// Адрес миниатюры подписан сроком и в каждом ответе сервера новый, а кадр тот
// же. Держим первый адрес: иначе браузер тянул бы картинку заново на каждую
// перерисовку списка, и строки мигали бы.
const thumbUrls = new Map();
function thumbOf(j) {
  if (!j.thumb_url) return "";
  if (!thumbUrls.has(j.id)) thumbUrls.set(j.id, j.thumb_url);
  return thumbUrls.get(j.id);
}

function jobThumb(j) {
  const box = el("span", { class: "thumb" }, [icon(isActive(j) ? "clock" : "film", 20)]);
  const url = thumbOf(j);
  if (url) {
    const img = el("img", { src: url, alt: "", loading: "lazy", decoding: "async" });
    img.addEventListener("load", () => box.classList.add("has-image"));
    // Подпись истекла или кадр не снялся — остаётся значок, а адрес спросим заново.
    img.addEventListener("error", () => { img.remove(); thumbUrls.delete(j.id); });
    box.append(img);
  }
  return box;
}

const playable = (j) => j.status === "done" && !!j.video_url && !j.purged;

// Нажатие на ролик: готовый открывается в плеере, остальные ведут в «Результаты».
function openJob(j) {
  if (playable(j)) openPlayer(j);
  else go("videos");
}

async function sendToChat(j, btn) {
  if (btn) btn.disabled = true;
  try {
    await api(`/api/jobs/${j.id}/send`, { method: "POST" });
    haptic("success");
    toast(tr("jobs.sent"));
  } catch (e) {
    haptic("error");
    popup(tr("jobs.not_sent"), e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openPlayer(j) {
  haptic();
  openSheet(jobTitle(j), [
    // `#t=0.1` — пока ролик не запущен, плеер показывает кадр, а не чёрный прямоугольник.
    el("video", { class: "player", src: `${j.video_url}#t=0.1`, controls: true, playsinline: true, preload: "metadata" }),
    el("p", { class: "muted small player__meta", text: jobSub(j) }),
    el("button", { class: "cta cta--slim", type: "button", onclick: (ev) => sendToChat(j, ev.currentTarget) }, [
      el("span", { class: "cta__title", text: tr("jobs.send") }),
    ]),
  ]);
}

function renderHome() {
  const box = $("#home-recent");
  if (!state.jobsLoaded) {
    box.replaceChildren(el("div", { class: "skeleton skeleton--flat" }));
    return;
  }
  const recent = state.jobs.slice(0, 3);
  if (!recent.length) {
    box.replaceChildren(el("button", { class: "list-row", type: "button", onclick: () => go("studio") }, [
      el("span", { class: "thumb" }, [icon("film", 20)]),
      el("span", { class: "list-row__text" }, [el("b", { text: tr("home.empty") }), el("span", { class: "muted", text: tr("home.empty_sub") })]),
      icon("chevron", 18, "chev"),
    ]));
    return;
  }
  box.replaceChildren(...recent.map((j) =>
    el("button", { class: "list-row", type: "button", onclick: () => openJob(j) }, [
      jobThumb(j),
      el("span", { class: "list-row__text" }, [el("b", { text: jobTitle(j) }), el("span", { class: "muted", text: jobSub(j) })]),
      icon("chevron", 18, "chev"),
    ])));
}

// ------------------------------------------------------------------ «Студия»

// Экран телефона в карточке: кадр последнего готового ролика. Ставится только
// после загрузки картинки — до неё (и если кадра нет) виден нарисованный.
function renderStudio() {
  const phone = $("#studio-phone");
  const last = state.jobs.find((j) => j.thumb_url);
  const url = last ? thumbOf(last) : "";
  if (phone.dataset.url === url) return;
  phone.dataset.url = url;
  phone.classList.remove("has-image");
  phone.style.backgroundImage = "";
  if (!url) return;
  const img = new Image();
  img.onload = () => {
    if (phone.dataset.url !== url) return;
    phone.style.backgroundImage = `url("${url}")`;
    phone.classList.add("has-image");
  };
  img.src = url;
}

// ------------------------------------------------------------------ «Результаты»

const JOB_FILTERS = {
  all: () => true,
  done: (j) => j.status === "done",
  active: (j) => isActive(j),
};

function renderJobFilter() {
  $("#jobs-filter").replaceChildren(...Object.keys(JOB_FILTERS).map((key) => {
    const n = state.jobs.filter(JOB_FILTERS[key]).length;
    return el("button", {
      type: "button", role: "radio", class: key === state.jobsFilter ? "is-active" : "",
      "aria-checked": key === state.jobsFilter ? "true" : "false",
      onclick: () => { state.jobsFilter = key; haptic(); renderJobs(); },
    }, [tr(`jobs.${key}`), state.jobsLoaded && n ? el("b", { text: String(n) }) : null]);
  }));
}

function renderJobs() {
  const box = $("#jobs");
  renderJobFilter();
  if (!state.jobsLoaded) {
    box.replaceChildren(...[0, 1, 2].map(() => el("div", { class: "skeleton" })));
    return;
  }
  const jobs = state.jobs.filter(JOB_FILTERS[state.jobsFilter]);
  if (!jobs.length) {
    const none = !state.jobs.length;
    box.replaceChildren(el("div", { class: "card empty" }, [
      el("b", { text: tr(none ? "jobs.empty" : "jobs.empty_filter") }),
      none ? el("p", { class: "muted", text: tr("jobs.empty_hint") }) : null,
      el("button", { class: "btn-soft", type: "button", text: tr("jobs.to_edit"), onclick: () => go("edit") }),
    ]));
    return;
  }
  // Ролики идут по дням: над первым роликом каждого дня — его дата.
  const out = [];
  let day = null;
  for (const j of jobs) {
    const d = j.created_at ? new Date(j.created_at).toDateString() : "";
    if (d !== day) {
      day = d;
      if (d) out.push(el("div", { class: "day-label", text: fmtDay(j.created_at) }));
    }
    out.push(jobRow(j));
  }
  box.replaceChildren(...out);
}

// Строка «Результатов»: миниатюра, название, срок хранения и кнопка «прислать
// в чат». У ролика в работе под строкой — этап и полоса, у упавшего — причина.
function jobRow(j) {
  const text = el("span", { class: "list-row__text" }, [
    el("b", { text: jobTitle(j) }),
    el("span", { class: "muted", text: jobSub(j) }),
  ]);
  const ready = playable(j);
  const main = ready
    ? el("button", { class: "job-row__main", type: "button", onclick: () => openPlayer(j) }, [jobThumb(j), text])
    : el("div", { class: "job-row__main" }, [jobThumb(j), text]);
  const row = el("div", { class: `card job-row job-row--${j.status}` }, [main]);
  if (ready) {
    row.append(el("button", {
      class: "round-act", type: "button", "aria-label": tr("jobs.send"), title: tr("jobs.send"),
      onclick: (ev) => sendToChat(j, ev.currentTarget),
    }, [icon("download", 18)]));
  }
  const more = [];
  if (isActive(j)) {
    if (j.stage) more.push(el("div", { class: "muted", text: j.stage }));
    more.push(el("div", { class: "bar" }, [el("i", { style: `width:${Math.round((j.progress || 0) * 100)}%` })]));
  } else if (j.status === "failed") {
    if (j.error) more.push(el("div", { class: "muted", text: j.error }));
    if (pays(state.boot.user)) more.push(el("div", { class: "muted", text: tr("jobs.refunded") }));
  } else if (j.purged) {
    more.push(el("div", { class: "muted", text: tr("jobs.purged") }));
  }
  if (more.length) row.append(el("div", { class: "job-row__more" }, more));
  return row;
}

function renderActivity() {
  const active = state.jobs.some(isActive);
  for (const dot of $$("[data-active-dot]")) dot.hidden = !active;
  renderBell();
}

// ------------------------------------------------------------------ уведомления

// Колокольчик в шапке: готовые и упавшие ролики, свежие сверху. Точка горит,
// пока есть ролик новее последнего просмотренного — отметка лежит на устройстве.
const NOTIF_KEY = "reel-notif-seen";

function notifItems() {
  return state.jobs.filter((j) => j.status === "done" || j.status === "failed").slice(0, 20);
}
function notifSeen() {
  try { return localStorage.getItem(NOTIF_KEY) || ""; } catch (_) { return ""; }
}
function renderBell() {
  const latest = notifItems()[0];
  $("#bell-dot").hidden = !(latest && latest.created_at && latest.created_at > notifSeen());
}

function openNotifications() {
  haptic();
  const items = notifItems();
  if (items[0] && items[0].created_at) {
    try { localStorage.setItem(NOTIF_KEY, items[0].created_at); } catch (_) { /* не критично */ }
  }
  renderBell();
  if (!items.length) {
    openSheet(tr("notif.title"), el("p", { class: "muted empty-line", text: tr("notif.empty") }));
    return;
  }
  openSheet(tr("notif.title"), items.map((j) => {
    const failed = j.status === "failed";
    const when = [fmtWhen(j.created_at), failed && pays(state.boot.user) ? tr("notif.failed_sub") : null].filter(Boolean).join(" · ");
    return el("button", {
      class: "history-row", type: "button",
      onclick: () => { closeSheet(); openJob(j); },
    }, [
      jobThumb(j),
      el("span", { class: "list-row__text" }, [
        el("b", { text: tr(failed ? "notif.failed" : "notif.done") }),
        el("span", { class: "muted", text: when }),
      ]),
      icon("chevron", 18, "chev"),
    ]);
  }));
}

async function loadJobs() {
  try {
    const data = await api("/api/jobs");
    const sig = (list) => JSON.stringify(list.map((j) => [j.id, j.status, j.progress, j.stage]));
    const changed = !state.jobsLoaded || sig(data.jobs) !== sig(state.jobs);
    state.jobs = data.jobs;
    state.jobsLoaded = true;
    state.boot.user.balance = data.balance;
    renderBalance();
    renderActivity();
    // Перерисовываем только при изменениях: иначе плеер готового ролика
    // сбрасывался бы каждые три секунды посреди просмотра.
    if (changed) {
      if (state.screen === "videos") renderJobs();
      if (state.screen === "home") renderHome();
      if (state.screen === "studio") renderStudio();
    }
  } catch (e) {
    if (state.screen === "videos") toast(e.message);
  }
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (state.jobs.some(isActive)) state.pollTimer = setTimeout(loadJobs, 3000);
}

// ------------------------------------------------------------------ «Тарифы»

// Пакеты токенов как их прислал сервер: «популярный» и честная выгода
// посчитаны там (`server._packages_view`), здесь только рисуем.
function packageFacts() {
  return state.boot.packages || [];
}

function renderTariffs() {
  const facts = packageFacts();
  if (state.pkg === null || !facts.some((p) => p.tokens === state.pkg)) {
    const pop = facts.find((p) => p.popular) || facts[0];
    state.pkg = pop ? pop.tokens : null;
  }
  $("#packages").replaceChildren(...facts.map((p) => {
    const on = p.tokens === state.pkg;
    return el("button", {
      class: "package", type: "button", role: "radio", "aria-checked": on ? "true" : "false",
      onclick: () => { state.pkg = p.tokens; haptic(); renderTariffs(); },
    }, [
      el("span", { class: `radio${on ? " is-on" : ""}` }),
      el("span", { class: "package__main" }, [
        p.popular ? el("span", { class: "badge", text: tr("pkg.popular") }) : null,
        el("span", { class: "package__amount" }, [el("b", { text: String(p.tokens) }), el("span", { class: "muted", text: tokensWord(p.tokens) })]),
        el("span", { class: "muted", text: tr("pkg.per", { n: p.videos, w: videosWord(p.videos) }) }),
      ]),
      el("span", { class: "package__price" }, [
        el("b", { text: `${p.stars} ⭐` }),
        p.rub ? el("span", { class: "muted", text: tr("pkg.rub", { n: fmtRub(p.rub) }) }) : null,
        p.save > 0 ? el("span", { class: "package__save", text: tr("pkg.save", { n: p.save }) }) : null,
      ]),
    ]);
  }));
  const chosen = facts.find((p) => p.tokens === state.pkg);
  const method = renderPayMethods(chosen);
  const stars = method && method.key === "stars";
  $("#pay-title").textContent = !chosen || !method ? ""
    : (stars ? tr("pay.button", { n: chosen.stars })
      : tr("pay.button_rub", { n: `${method.approx ? "≈ " : ""}${fmtRub(chosen.rub)}` }));
  $("#pay-sub").textContent = chosen ? tr("pay.sub", { n: chosen.tokens, w: tokensWord(chosen.tokens) }) : "";
  $("#pay").hidden = !chosen || !method;
}

// Способы оплаты как их прислал сервер: PayHot (карта, СБП, крипта) и звёзды.
// Способ с порогом выше цены пакета виден, но выбрать его нельзя — подпись
// «от N ₽» объясняет почему. → выбранный способ или null.
function renderPayMethods(pkg) {
  const list = state.boot.pay_methods || [{ key: "stars", kind: "stars", min_rub: 0 }];
  const usable = (m) => !pkg || !m.min_rub || (pkg.rub || 0) >= m.min_rub;
  if (!list.some((m) => m.key === state.payMethod && usable(m))) {
    const first = list.find(usable);
    state.payMethod = first ? first.key : null;
  }
  $("#pay-methods").replaceChildren(...list.map((m) => {
    const ok = usable(m);
    const on = ok && m.key === state.payMethod;
    const rub = pkg && pkg.rub ? `${m.approx ? "≈ " : ""}${fmtRub(pkg.rub)} ₽` : "";
    const price = !pkg ? "" : (m.kind === "stars" ? `${pkg.stars} ⭐` : rub);
    return el("button", {
      class: `pay-method${ok ? "" : " is-off"}`, type: "button", role: "radio",
      "aria-checked": on ? "true" : "false", "aria-disabled": ok ? "false" : "true",
      onclick: () => { if (!ok) return; state.payMethod = m.key; haptic(); renderTariffs(); },
    }, [
      el("span", { class: `radio${on ? " is-on" : ""}` }),
      el("span", { class: "pay-method__name", text: tr(`method.${m.key}`) }),
      el("span", { class: "muted pay-method__price", text: ok ? price : tr("method.from", { n: fmtRub(m.min_rub) }) }),
    ]);
  }));
  return list.find((m) => m.key === state.payMethod) || null;
}

// Оплата через PayHot: счёт на сервере → страница оплаты в браузере → статус
// спрашиваем у сервера, а тот — у PayHot. Возврат из браузера сам по себе
// ничего не подтверждает; зачисляет сервер (и фоновый опрос в боте, если
// приложение закроют).
let payhotWatch = null;
async function buyPayhot(tokens, method) {
  haptic();
  toast(tr("pay.opening"));
  let order;
  try {
    order = await api("/api/payhot/orders", { method: "POST", body: { tokens, method } });
  } catch (e) {
    popup(tr("err.title"), e.message);
    return;
  }
  if (tg && tg.openLink) tg.openLink(order.url);
  else window.open(order.url, "_blank");
  watchPayhot(order.order_id, tokens);
}

function watchPayhot(orderId, tokens) {
  if (payhotWatch) payhotWatch.stop();
  const started = Date.now();
  let timer = null;
  let stopped = false;
  const stop = () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", now); };
  const check = async () => {
    if (stopped) return;
    try {
      const st = await api(`/api/payhot/orders/${orderId}`);
      if (st.status === "succeeded") {
        stop();
        haptic("success");
        await refreshBoot();
        toast(tr("pay.credited", { n: tokens, w: tokensWord(tokens) }));
        return;
      }
      if (st.final) {
        stop();
        if (st.status !== "mismatch") popup(tr("pay.failed"), tr("pay.not_paid"));
        return;
      }
    } catch (_) { /* сеть мигнула — спросим ещё раз */ }
    // Счёт PayHot живёт до часа; дальше доспросит фоновый опрос в боте.
    if (Date.now() - started < 60 * 60 * 1000) timer = setTimeout(check, 4000);
  };
  // Вернулись из браузера — спрашиваем сразу, не дожидаясь таймера.
  const now = () => { if (document.visibilityState === "visible") { clearTimeout(timer); check(); } };
  document.addEventListener("visibilitychange", now);
  payhotWatch = { stop };
  toast(tr("pay.waiting"));
  timer = setTimeout(check, 4000);
}

async function buy(tokens) {
  haptic();
  if (!tg || !tg.openInvoice) { popup(tr("pay.title"), tr("pay.update")); return; }
  let link;
  try {
    link = (await api("/api/invoice", { method: "POST", body: { tokens } })).link;
  } catch (e) {
    popup(tr("err.title"), e.message);
    return;
  }
  const before = state.boot.user.balance;
  tg.openInvoice(link, async (status) => {
    if (status !== "paid") {
      if (status === "failed") popup(tr("pay.failed"), tr("pay.failed_text"));
      return;
    }
    haptic("success");
    toast(tr("pay.crediting"));
    // Зачисляет бот по сообщению Telegram об оплате, а не этот ответ клиента,
    // поэтому ждём, пока баланс действительно вырастет.
    for (let i = 0; i < 15; i += 1) {
      await refreshBoot();
      if (state.boot.user.balance > before) { toast(tr("pay.credited", { n: tokens, w: tokensWord(tokens) })); return; }
      await new Promise((r) => setTimeout(r, 1000));
    }
    toast(tr("pay.later"));
  });
}

// ------------------------------------------------------------------ лист снизу

let sheetReturnFocus = null;
function openSheet(title, body) {
  sheetReturnFocus = document.activeElement;
  $("#sheet-title").textContent = title;
  $("#sheet-body").replaceChildren(...[].concat(body));
  $("#sheet").hidden = false;
  syncBackButton();
  const close = $("#sheet [data-sheet-close].icon-btn");
  if (close) close.focus();
}
function closeSheet() {
  if ($("#sheet").hidden) return false;
  $("#sheet").hidden = true;
  // Содержимое убираем: плеер в закрытом листе продолжал бы играть.
  $("#sheet-body").replaceChildren();
  syncBackButton();
  if (sheetReturnFocus && sheetReturnFocus.focus) sheetReturnFocus.focus();
  return true;
}

async function openHistory() {
  haptic();
  openSheet(tr("history.title"), el("div", { class: "skeleton" }));
  let items;
  try {
    items = (await api("/api/payments")).items;
  } catch (e) {
    $("#sheet-body").replaceChildren(el("p", { class: "muted", text: e.message }));
    return;
  }
  if (!items.length) {
    $("#sheet-body").replaceChildren(el("p", { class: "muted empty-line", text: tr("history.empty") }));
    return;
  }
  $("#sheet-body").replaceChildren(...items.map((it) => {
    const bonus = it.kind === "bonus";
    return el("div", { class: "history-row" }, [
      el("span", { class: "tile" }, [icon(bonus ? "invite" : "star", 18, bonus ? null : "star")]),
      el("span", { class: "list-row__text" }, [
        el("b", { text: tr(bonus ? "history.bonus" : "history.payment") }),
        el("span", { class: "muted", text: fmtWhen(it.at) }),
      ]),
      el("span", { class: "list-row__end" }, [
        el("b", { class: "pos", text: bonus ? `+${fmtShare(it.share)}` : `+${it.tokens}` }),
        el("span", { class: "muted", text: bonus ? tokensWord(2) : (it.stars ? `${it.stars} ⭐` : `${fmtRub(it.rub || 0)} ₽`) }),
      ]),
    ]);
  }));
}

// ------------------------------------------------------------------ «Приглашения»

function referralLink() {
  const r = state.boot.referral;
  return r ? `https://t.me/${state.boot.bot_username}?start=${r.code}` : "";
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // WebView Telegram на части клиентов не пускает в буфер обмена через
    // Clipboard API — старый путь через выделение работает почти везде.
    const area = el("textarea", { style: "position:fixed;opacity:0" });
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    area.remove();
    return ok;
  }
}

function shareInvite() {
  const share = `https://t.me/share/url?url=${encodeURIComponent(referralLink())}&text=${encodeURIComponent(tr("invite.text"))}`;
  haptic();
  openTelegram(share);
}

function renderInviteStatic() {
  const r = state.boot.referral;
  if (!r) return;
  $("#invite-lead").textContent = tr("invite.lead", { p: r.percent });
  $("#invite-link").textContent = referralLink();
  const credit = $("#ref-credit");
  credit.hidden = !(r.credit > 0);
  credit.textContent = tr("invite.credit", { n: fmtShare(r.credit) });
  $("#ref-period-text").textContent = tr(`period.${state.refDays}`);
  const p = state.boot.partner_percent || 0;
  $("#partner-sub").textContent = tr("partner.sub", { p });
  $("#payout-foot").textContent = tr("partner.payout_foot", { p });
}

// Период списка приглашённых — выбор из трёх в листе снизу.
function openPeriod() {
  haptic();
  openSheet(tr("period.title"), [7, 30, 0].map((d) =>
    el("button", {
      class: `option-row${d === state.refDays ? " is-active" : ""}`, type: "button",
      onclick: () => {
        closeSheet();
        if (state.refDays === d) return;
        state.refDays = d;
        haptic();
        renderInviteStatic();
        loadReferrals();
      },
    }, [el("span", { text: tr(`period.${d}`) }), d === state.refDays ? icon("check", 18) : null])));
}

// «Стать партнёром»: выплаты деньгами оформляет живой человек — ведём к нему.
function becomePartner() {
  haptic();
  const link = state.boot.links && state.boot.links.support;
  if (link) openLink(link);
  else popup(tr("partner.become"), tr("partner.soon"));
}
function partnerTerms() {
  haptic();
  popup(tr("partner.title"), tr("partner.terms_text", { p: state.boot.partner_percent || 0 }));
}

function renderReferrals() {
  const data = state.refs;
  if (!data) {
    $("#ref-friends").replaceChildren(el("div", { class: "skeleton" }));
    return;
  }
  const s = data.stats;
  $("#ref-invited").textContent = s.invited;
  $("#ref-paid").textContent = s.paid;
  $("#ref-earned").textContent = `+${fmtShare(s.earned)}`;
  $("#ref-invited-today").textContent = tr("invite.today", { n: s.invited_today });
  $("#ref-paid-today").textContent = tr("invite.today", { n: s.paid_today });
  $("#ref-earned-today").textContent = tr("invite.today", { n: fmtShare(s.earned_today) });

  const friends = data.friends;
  const paid = friends.filter((f) => f.paid).length;
  const earned = friends.reduce((sum, f) => sum + f.earned, 0);
  $("#ref-summary").replaceChildren(
    el("b", { text: String(friends.length) }), ` ${tr("invite.sum_came")} · `,
    el("b", { text: String(paid) }), ` ${tr("invite.sum_paid")} · `,
    el("b", { class: "pos", text: `+${fmtShare(earned)} ${tr("invite.sum_tokens")}` }),
  );
  if (!friends.length) {
    $("#ref-friends").replaceChildren(el("p", {
      class: "muted friends-empty", text: tr(state.refDays ? "invite.none_period" : "invite.none_all"),
    }));
    return;
  }
  $("#ref-friends").replaceChildren(...friends.map((f) => {
    const name = f.name || tr("invite.noname");
    return el("div", { class: "list-row" }, [
      el("span", { class: "avatar", text: name.slice(0, 1).toUpperCase() }),
      el("span", { class: "list-row__text" }, [el("b", { text: name }), el("span", { class: "muted", text: fmtDate(f.joined_at) })]),
      el("span", { class: "list-row__end" }, f.paid
        ? [el("b", { class: "pos", text: `+${fmtShare(f.earned)}` }), el("span", { class: "muted", text: tr("invite.friend_paid") })]
        : [el("span", { class: "muted", text: tr("invite.friend_waiting") })]),
    ]);
  }));
}

async function loadReferrals() {
  if (!state.boot.referral) return;
  const days = state.refDays;
  try {
    const data = await api(`/api/referrals?days=${days}&tz=${new Date().getTimezoneOffset()}`);
    if (days !== state.refDays) return; // пока шёл ответ, выбрали другой период
    state.refs = data;
    renderReferrals();
  } catch (e) {
    if (state.screen === "invite") toast(e.message);
  }
}

// ------------------------------------------------------------------ «Профиль» и общее

function renderUser() {
  const u = state.boot.user;
  const name = u.first_name || u.username || tr("profile.noname");
  for (const n of $$("[data-user-name]")) n.textContent = name;
  for (const n of $$("[data-user-handle]")) n.textContent = u.username ? `@${u.username}` : "";
  // Фото из Telegram, а без него — первая буква имени.
  const me = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
  const photo = me && /^https:\/\//.test(me.photo_url || "") ? me.photo_url : "";
  for (const n of $$("[data-avatar]")) {
    n.textContent = photo ? "" : name.slice(0, 1).toUpperCase();
    n.classList.toggle("has-photo", !!photo);
    n.style.backgroundImage = photo ? `url("${encodeURI(photo)}")` : "";
  }
  for (const brand of $$("[data-brand]")) {
    brand.replaceChildren(state.boot.bot_name || "Reels", el("i", { class: "grad-dot", text: "." }));
  }
  for (const n of $$("[data-needs='referral']")) n.hidden = !state.boot.referral;
  const p = state.boot.referral ? state.boot.referral.percent : 0;
  $("#profile-invite-sub").textContent = tr("profile.invite_sub", { p });
}

function renderProfile() {
  const u = state.boot.user;
  const links = state.boot.links || {};
  $("#profile-topup").hidden = u.is_admin;
  const hint = $("#profile-hint");
  hint.hidden = !u.is_admin;
  hint.textContent = u.is_admin ? tr("profile.hint_admin") : "";
  $("#profile-partner-sub").textContent = tr("partner.row_sub", { p: state.boot.partner_percent || 0 });
  // Канал и оферта — только с адресом: строка в никуда хуже отсутствующей.
  $("#open-channel").hidden = !links.channel;
  $("#open-offer").hidden = !links.offer;
}

function renderBalance() {
  const u = state.boot.user;
  const n = u.balance;
  for (const node of $$("[data-balance-value]")) node.textContent = u.is_admin ? "∞" : n;
  for (const node of $$("[data-balance-unit]")) node.textContent = u.is_admin ? tr("balance.unlimited") : tokensWord(n);
  for (const node of $$("[data-balance-count]")) node.textContent = u.is_admin ? "∞" : n;
  $("#balance-note").textContent = tr(
    u.is_admin ? "balance.note_admin" : "balance.note",
    { c: tokensText(videoCost()) },
  );
  renderCta();
}

function renderLanguages() {
  const current = currentLang();
  for (const box of $$("[data-lang-seg]")) {
    box.replaceChildren(...["ru", "en"].map((code) =>
      el("button", {
        type: "button", class: code === current ? "is-active" : "", text: code.toUpperCase(),
        "aria-pressed": code === current ? "true" : "false",
        onclick: () => setLanguage(code),
      })));
  }
}

async function setLanguage(code) {
  if (code === currentLang()) return;
  haptic();
  const previous = state.boot.user.lang;
  state.boot.user.lang = code;
  rerenderAll();
  try {
    await api("/api/language", { method: "POST", body: { lang: code } });
    // Названия цветов и стадий приходят с сервера на языке пользователя —
    // перечитываем.
    const fresh = await api("/api/bootstrap");
    state.boot.accents = fresh.accents;
    state.boot.user = fresh.user;
    rerenderAll();
    loadJobs();
  } catch (e) {
    state.boot.user.lang = previous;
    rerenderAll();
    popup(tr("err.title"), e.message);
  }
}

function rerenderAll() {
  applyStaticTexts();
  sidebarTitles();
  renderUser();
  renderLanguages();
  renderThemeSeg();
  renderPresets();
  renderAccents();
  renderOptions();
  renderLimits();
  renderNext();
  renderBalance();
  renderHeader();
  preview.render();
  if (state.file) setFile(state.file);
  if (state.topFile) setFile(state.topFile, "top");
  renderScreen(state.screen);
}

// ------------------------------------------------------------------ навигация

const TITLES = { home: "nav.home", invite: "title.invite", studio: "nav.studio", edit: "nav.edit", videos: "nav.videos", tariffs: "nav.tariffs", profile: "nav.profile" };

const isRoot = () => ROOT_SCREENS.includes(state.screen);

function renderHeader() {
  // Вкладка: логотип, тема, уведомления, баланс. Вложенный экран: «назад»,
  // название (у монтажа ещё шаг) и баланс.
  const root = isRoot();
  document.documentElement.classList.toggle("is-sub", !root);
  $("#topbar-title").hidden = root;
  for (const n of $$("[data-root-only]")) n.hidden = !root;
  $("#page-title").textContent = tr(TITLES[state.screen] || "nav.home");
  const step = $("#page-step");
  step.hidden = state.screen !== "edit";
  step.textContent = tr("step", { n: state.step });
  document.title = `${tr(TITLES[state.screen] || "nav.home")} · ${(state.boot && state.boot.bot_name) || "Reels"}`;
}

function canGoBack() {
  return !$("#sheet").hidden || !isRoot() || state.history.length > 0;
}

function goBack() {
  if (closeSheet()) return;
  if (state.screen === "edit" && state.step === 2) { setStep(1); return; }
  const prev = state.history.pop();
  // Вложенный экран открыли сразу (кнопкой из чата) — возвращаемся туда,
  // откуда в него попадают: монтаж из «Студии», тарифы с главной.
  go(prev || (state.screen === "edit" ? "studio" : "home"), { push: false });
}

// Две кнопки «назад» — Telegram и своя в шапке — живут от одного условия:
// иначе открытый лист «Истории» закрывался одной и не закрывался другой.
function syncBackButton() {
  // Своя «назад» — только на вложенных экранах: на вкладках её место занято
  // логотипом, там возврат делает кнопка Telegram.
  $("#back").hidden = isRoot();
  if (!tg || !tg.BackButton) return;
  if (canGoBack()) tg.BackButton.show(); else tg.BackButton.hide();
}

function renderScreen(screen) {
  if (screen === "home") renderHome();
  if (screen === "studio") renderStudio();
  if (screen === "videos") renderJobs();
  if (screen === "tariffs") renderTariffs();
  if (screen === "profile") renderProfile();
  if (screen === "invite") { renderInviteStatic(); renderReferrals(); }
}

function go(screen, { push = true, replace = false } = {}) {
  screen = SCREEN_ALIASES[screen] || screen;
  if (!SCREENS.includes(screen)) screen = "home";
  if (screen === "invite" && !state.boot.referral) { shareInvite(); return; }
  closeSheet();
  if (screen !== state.screen) {
    // Главная — корень: дойдя до неё, историю дальше не копим, иначе «назад»
    // водил бы кругами по всем когда-то открытым вкладкам.
    if (screen === "home") state.history = [];
    else if (push && !replace && state.screen) state.history.push(state.screen);
    state.screen = screen;
  }
  for (const s of $$("[data-screen]")) s.hidden = s.dataset.screen !== screen;
  for (const b of $$("[data-nav]")) b.classList.toggle("is-active", b.dataset.nav === (NAV_OF[screen] || screen));
  syncDesignMode();
  window.scrollTo({ top: 0 });
  renderHeader();
  syncBackButton();
  syncPreview();
  renderScreen(screen);
  if (screen === "videos" || screen === "home" || screen === "studio") loadJobs();
  if (screen === "invite") loadReferrals();
  if (screen === "edit" && state.step === 2) setStep(2);
}

async function refreshBoot() {
  const fresh = await api("/api/bootstrap");
  state.boot.user = fresh.user;
  state.boot.referral = fresh.referral;
  state.boot.links = fresh.links;
  renderBalance();
  renderUser();
}

const COLLAPSE_KEY = "reel-sidebar";
function initSidebar() {
  const shell = $("#app");
  let saved = null;
  try { saved = localStorage.getItem(COLLAPSE_KEY); } catch (_) { /* приватный режим */ }
  shell.classList.toggle("is-collapsed", saved === "1");
  $("#collapse").addEventListener("click", () => {
    const next = !shell.classList.contains("is-collapsed");
    shell.classList.toggle("is-collapsed", next);
    try { localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0"); } catch (_) { /* не критично */ }
    haptic();
  });
  sidebarTitles();
}

// В свёрнутой панели подписи спрятаны — пусть живут во всплывающей подсказке.
// Отдельно, потому что язык меняется на ходу, и подсказки должны идти за ним.
function sidebarTitles() {
  for (const b of $$(".side-nav__item")) {
    const label = b.querySelector("[data-i18n]");
    if (label) b.title = label.textContent;
  }
}

async function main() {
  initTheme();
  applyStaticTexts();
  if (!tg || !tg.initData) {
    $("#blocker").hidden = false;
    return;
  }
  tg.ready();
  tg.expand();
  try { tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch (_) { /* старый клиент */ }
  if (tg.BackButton) tg.BackButton.onClick(goBack);

  try {
    state.boot = await api("/api/bootstrap");
  } catch (e) {
    $("#blocker").hidden = false;
    $("#blocker h2").textContent = tr("blocker.failed");
    // 401 — подпись Telegram не прошла: причина с сервера на русском и пользователю
    // ничего не скажет, а «откройте из бота» — единственное, что он может сделать.
    $("#blocker p").textContent = e.status === 401 ? tr("blocker.text") : e.message;
    return;
  }
  const b = state.boot;
  applyStaticTexts();
  state.preset = b.presets.some((p) => p.name === b.user.preset) ? b.user.preset : (b.presets[0] || {}).name;
  state.options = { ...b.defaults };
  state.layout = loadLayout();
  // Запомненный выбор важнее умолчания: тема уже так и работает (`user.preset`),
  // а цвет до сих пор сбрасывался на каждом открытии приложения.
  const saved = (b.user && b.user.settings) || {};
  state.accent = saved.accent || b.defaults.accent || "theme";
  // Переключатели эффектов помнятся так же (`options.REMEMBERED` на сервере).
  // Нарезка — нет: её выбирают под конкретное видео на первом шаге.
  for (const input of $$("[data-option]")) {
    const key = input.dataset.option;
    if (typeof saved[key] === "boolean") state.options[key] = saved[key];
  }

  declareFonts();
  renderUser();
  renderLanguages();
  renderPresets();
  renderAccents();
  renderOptions();
  renderLimits();
  renderNext();
  renderBalance();
  initEdit();
  initStrips();
  initSidebar();
  // Ширина строки и подписи плиток меряются шрифтами тем — до их загрузки
  // замер идёт по подставному системному и врёт в меньшую сторону: названия
  // приезжали обрезанными многоточием ровно на первом, холодном открытии.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      syncAllArrows();
      preview.render();
    });
  }
  // Превью стоит на месте и одного размера — но размер этот зависит от высоты
  // окна (клавиатура, поворот, панель Telegram). Кегль субтитра считается от
  // высоты кадра, поэтому на смене размера кадр перерисовывается.
  if (window.ResizeObserver) {
    let h = 0;
    new ResizeObserver(([e]) => {
      const nh = Math.round(e.contentRect.height);
      if (nh && nh !== h) { h = nh; preview.render(); }
    }).observe($("#preview"));
  }
  syncTabbarHeight();
  // Вкладка в фоне — таймер не нужен; возврат к ней пересчитывает кадр.
  document.addEventListener("visibilitychange", syncPreview);

  for (const btn of $$("[data-nav]")) btn.addEventListener("click", () => { haptic(); go(btn.dataset.nav); });
  $("#back").addEventListener("click", () => { haptic(); goBack(); });
  for (const n of $$("[data-sheet-close]")) n.addEventListener("click", () => closeSheet());
  addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeSheet(); });
  $("#history-open").addEventListener("click", openHistory);
  $("#pay").addEventListener("click", () => {
    if (!state.pkg || !state.payMethod) return;
    if (state.payMethod === "stars") buy(state.pkg);
    else buyPayhot(state.pkg, state.payMethod);
  });
  $("#invite-copy").addEventListener("click", async () => {
    const ok = await copyText(referralLink());
    haptic(ok ? "success" : "error");
    toast(ok ? tr("invite.copied") : referralLink());
  });
  $("#invite-share").addEventListener("click", shareInvite);
  $("#ref-period").addEventListener("click", openPeriod);
  $("#partner-go").addEventListener("click", becomePartner);
  $("#profile-partner").addEventListener("click", becomePartner);
  $("#partner-terms").addEventListener("click", partnerTerms);
  $("#bell").addEventListener("click", openNotifications);
  const botChat = () => `https://t.me/${state.boot.bot_username}`;
  const links = () => state.boot.links || {};
  $("#open-chat").addEventListener("click", () => { haptic(); openTelegram(botChat()); });
  $("#open-channel").addEventListener("click", () => { haptic(); openLink(links().channel); });
  // Без контакта поддержки пишут самому боту: там есть кнопка «Поддержка».
  $("#open-support").addEventListener("click", () => { haptic(); openLink(links().support || botChat()); });
  $("#open-offer").addEventListener("click", () => { haptic(); openLink(links().offer); });

  // Вкладка приходит параметром `?screen=`, а не фрагментом: фрагмент занят
  // Telegram — в нём едут параметры запуска (`#tgWebAppData=…`).
  go(new URLSearchParams(location.search).get("screen") || "home", { push: false });
  if (!["home", "videos", "studio"].includes(state.screen)) loadJobs();
}

main();
