"""Опции одного монтажа — то, что пользователь выбирает перед загрузкой.

Сохраняются в задаче целиком (`jobs.options`) и читаются воркером: чат и Mini
App ставят задачи одним путём, а различаются только набором галочек.

Переключатели доходят до пайплайна тремя путями: нарезка, звуки, картинки,
камера и заголовок читаются прямо в `pipeline/job.py`; остальные сужают
настройки сервера на один ролик (`job_settings`); цветокоррекция правит пресет
(`apply_grade`).

Здесь нет ни одной опции, которую пайплайн не умеет выполнить. Экран-образец
предлагал ещё «хук», «эмодзи в субтитрах» и «плашки»: первых двух в пайплайне
нет (режиссёр хук находит, но не сочиняет, и эмодзи не расставляет), а плашки
сняты с субтитра по решению владельца. Галочка, которая ничего не меняет, хуже
отсутствующей.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.preset import Preset

# Цвет выделенного слова. Ключ — то, что хранится в задаче; значение — подписи
# на языках интерфейса и цвет. Порядок словаря — порядок чипов в приложении. `None` у цвета значит
# «как задумано темой»: у glass это приглушённое золото, у bold — салатовый.
ACCENTS: dict[str, tuple[dict[str, str], str | None]] = {
    "theme": ({"ru": "Как в теме", "en": "Theme default"}, None),
    "gold": ({"ru": "Золото", "en": "Gold"}, "#F3D28F"),
    "turquoise": ({"ru": "Бирюза", "en": "Turquoise"}, "#3DDBD9"),
    "violet": ({"ru": "Фиолет", "en": "Violet"}, "#A38BFF"),
    "neon_pink": ({"ru": "Розовый неон", "en": "Neon pink"}, "#FF4FD8"),
    "lime": ({"ru": "Лайм", "en": "Lime"}, "#C8F560"),
    "coral": ({"ru": "Коралл", "en": "Coral"}, "#FF7A59"),
    # Ниже — добавленные позже. Порядок словаря это порядок чипов в приложении,
    # поэтому новые идут в конец: у выбравших цвет ничего не переставляется.
    "red": ({"ru": "Красный", "en": "Red"}, "#FF3B30"),
    "sky": ({"ru": "Небо", "en": "Sky"}, "#4DA6FF"),
    "orange": ({"ru": "Апельсин", "en": "Orange"}, "#FF9F0A"),
    "mint": ({"ru": "Мята", "en": "Mint"}, "#30D158"),
    "amber": ({"ru": "Янтарь", "en": "Amber"}, "#FFD60A"),
    "ice": ({"ru": "Лёд", "en": "Ice"}, "#EAF2FF"),
}


# Понятные подписи тем. Название темы (`Preset.title`) — имя для своих: «Pulse»,
# «Glass», «Chisel» человеку, который видит их впервые, не говорят ничего, а тем
# семнадцать. Подпись отвечает на вопрос «как это выглядит». Ключ — имя темы;
# темы без подписи показываются по названию (`theme_label`), а тест следит,
# чтобы подпись была у каждой и чтобы две темы не назывались одинаково.
THEME_LABELS: dict[str, dict[str, str]] = {
    "pulse": {"ru": "С выделением слов", "en": "Highlighted words"},
    "glass": {"ru": "Крупные субтитры", "en": "Large captions"},
    "bold": {"ru": "Жирные заглавные", "en": "Bold caps"},
    "press": {"ru": "Журнальный", "en": "Magazine"},
    # Темы с парой шрифтов: мелкие слова одним, выделенное — другим.
    "script": {"ru": "Рукописный акцент", "en": "Handwritten accent"},
    "slant": {"ru": "Наклонный акцент", "en": "Slanted accent"},
    "marker": {"ru": "С маркером", "en": "Marker accent"},
    "cursive": {"ru": "С курсивом", "en": "Italic accent"},
    "widescreen": {"ru": "Как в кино", "en": "Cinematic"},
    "notebook": {"ru": "Печатная машинка", "en": "Typewriter"},
    "silk": {"ru": "Элегантный", "en": "Elegant"},
    "heavyweight": {"ru": "Очень крупный", "en": "Extra large"},
    "mariable": {"ru": "Мягкий с засечками", "en": "Soft serif"},
    "invitation": {"ru": "Нежный", "en": "Delicate"},
    "boardwalk": {"ru": "Афиша", "en": "Poster"},
    "deco_line": {"ru": "Тонкие заглавные", "en": "Thin caps"},
    "editorial": {"ru": "Газетный", "en": "Editorial"},
    "feather": {"ru": "Лёгкий", "en": "Light"},
    "nouveau": {"ru": "Винтажный", "en": "Vintage"},
    "modernist": {"ru": "Строгий", "en": "Clean caps"},
    "chisel": {"ru": "Классика", "en": "Classic caps"},
}


def theme_label(name: str, lang: str | None = None, fallback: str | None = None) -> str:
    """Подпись темы на языке интерфейса; нет подписи — название темы."""
    labels = THEME_LABELS.get(name)
    if not labels:
        return fallback or name
    return labels.get(lang or "ru") or labels["ru"]


class FontChoice(BaseModel):
    """Шрифт из библиотеки. Имя семейства — ровно то, что лежит внутри TTF.

    `family` сверяется с выгруженными метриками тестом: ASS-бэкенд ищет шрифт
    по имени семейства через fontconfig, и расхождение дало бы чужой шрифт без
    единого сообщения об ошибке.
    """

    model_config = ConfigDict(extra="forbid")

    family: str
    file: str
    weight: int


# Библиотека шрифтов субтитра. Ключ хранится в задаче, значение — подписи и сам
# шрифт. `None` значит «как задумано темой».
#
# Порядок записей — порядок чипов в приложении, и выстроен он по КОНТРАСТУ
# соседей, а не по времени добавления. На экран телефона помещается два с
# половиной чипа, и пока порядок был хронологическим, первыми человек видел
# два ОДИНАКОВЫХ образца: «как в теме» на большинстве тем это Montserrat, и
# сразу за ним «Гротеск» — тот же Montserrat. Лента выглядела так, будто
# шрифты не отличаются друг от друга.
#
# Ключи при перестановке не меняются, поэтому выбор, сохранённый в старых
# задачах и в `users.settings`, остаётся прежним.
#
# Добавить шрифт — это четыре шага, и пропуск любого ломает молча:
#   1. `python tools/font_instance.py <url|файл> <вес> --name <Имя>` (нарезать
#      статическое начертание: выгрузка метрик читает дефолтный инстанс, а у
#      переменных шрифтов он почти всегда Regular);
#   2. `python tools/font_metrics.py assets/fonts/<Имя>.ttf` и коммит метрик;
#   3. строка здесь;
#   4. прогон `pytest -m slow -k bundled` — сверка раскладки с Chromium.
#
# Здесь же заведены и начертания тем: тема несёт свой шрифт, но запирать его
# внутри темы незачем — антиква журнала с движением Pulse это другой ролик, и
# выбирается он здесь.
FONTS: dict[str, tuple[dict[str, str], FontChoice | None]] = {
    "theme": ({"ru": "Как в теме", "en": "Theme default"}, None),
    "serif": (
        {"ru": "Антиква", "en": "Serif"},
        FontChoice(
            family="Playfair Display ExtraBold",
            file="PlayfairDisplay-ExtraBold.ttf",
            weight=800,
        ),
    ),
    "mono": (
        {"ru": "Моноширинный", "en": "Monospace"},
        FontChoice(family="JetBrains Mono", file="JetBrainsMono-Bold.ttf", weight=700),
    ),
    "display": (
        {"ru": "Характерный", "en": "Display"},
        FontChoice(family="Unbounded ExtraBold", file="Unbounded-ExtraBold.ttf", weight=800),
    ),
    "slab": (
        {"ru": "Брусковый", "en": "Slab"},
        FontChoice(family="Bitter ExtraBold", file="Bitter-ExtraBold.ttf", weight=800),
    ),
    "narrow": (
        {"ru": "Узкий", "en": "Condensed"},
        FontChoice(family="Oswald SemiBold", file="Oswald-SemiBold.ttf", weight=600),
    ),
    "elegant": (
        {"ru": "Изящный", "en": "Elegant"},
        FontChoice(family="Cormorant SemiBold", file="Cormorant-SemiBold.ttf", weight=600),
    ),
    "wide": (
        {"ru": "Широкий", "en": "Wide"},
        FontChoice(family="Tenor Sans", file="TenorSans-Regular.ttf", weight=400),
    ),
    "festive": (
        {"ru": "Торжественный", "en": "Festive"},
        FontChoice(family="Yeseva One", file="YesevaOne-Regular.ttf", weight=400),
    ),
    "magazine": (
        {"ru": "Журнальный", "en": "Magazine"},
        FontChoice(family="Oranienbaum", file="Oranienbaum-Regular.ttf", weight=400),
    ),
    "geometric": (
        {"ru": "Геометрический", "en": "Geometric"},
        FontChoice(family="Jost SemiBold", file="Jost-SemiBold.ttf", weight=600),
    ),
    "roman": (
        {"ru": "Римский", "en": "Roman"},
        FontChoice(family="Forum", file="Forum-Regular.ttf", weight=400),
    ),
    "didone": (
        {"ru": "Дидон", "en": "Didone"},
        FontChoice(family="Prata", file="Prata-Regular.ttf", weight=400),
    ),
    "modern": (
        {"ru": "Модерн", "en": "Art nouveau"},
        FontChoice(family="Philosopher", file="Philosopher-Bold.ttf", weight=700),
    ),
    "light": (
        {"ru": "Лёгкий", "en": "Light"},
        FontChoice(family="Jost Light", file="Jost-Light.ttf", weight=300),
    ),
}


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accent: str = "theme"
    font: str = "theme"
    cut: bool = Field(default=True, description="резать затяжные паузы и заминки")
    sfx: bool = Field(default=True, description="звуковые акценты: щелчок на субтитре, картинки, укрупнения")
    broll: bool = Field(default=True, description="медиа-карточки по смыслу")
    camera: bool = Field(default=True, description="смена крупности на резах")
    # Ниже — переключатели, добавленные 27.11.2026. У каждого умолчание
    # «включено»: задача без поля ведёт себя как до его появления. Что именно
    # каждый выключает в пайплайне — `job_settings` и `apply_grade` ниже.
    studio: bool = Field(default=True, description="студийный звук: чистка, тембр и плотность голоса")
    tracking: bool = Field(default=True, description="кадр едет за головой")
    transitions: bool = Field(default=True, description="переходы на склейках и ударных словах")
    visuals: bool = Field(default=True, description="плашки, стикеры и сцены поверх ролика")
    title: bool = Field(default=True, description="заголовок-титр в первые секунды")
    progress: bool = Field(default=True, description="полоса прогресса сверху")
    grade: bool = Field(default=True, description="цветокоррекция темы")
    smooth: bool = Field(default=True, description="ролик в 60 кадров в секунду")

    @field_validator("accent")
    @classmethod
    def _known_accent(cls, v: str) -> str:
        return v if v in ACCENTS else "theme"

    @field_validator("font")
    @classmethod
    def _known_font(cls, v: str) -> str:
        return v if v in FONTS else "theme"

    @classmethod
    def for_new_job(cls, raw: dict[str, Any] | None) -> "JobOptions":
        """Опции НОВОГО ролика: шрифт всегда «как в теме».

        Отдельный выбор шрифта убран из интерфейса 16.11.2026: он перекрывал
        шрифт у всех стилей разом, и стили выглядели одинаково. Но выбор,
        сделанный раньше, лежит в `users.settings` и в присланных приложением
        опциях — без этого сброса он продолжал бы действовать, а убрать его
        человеку было бы уже нечем. Старые задачи читаются `from_stored` и свой
        шрифт сохраняют: перерендер обязан дать тот же ролик.
        """
        return cls.model_validate(raw or {}).model_copy(update={"font": "theme"})

    @classmethod
    def from_stored(cls, raw: dict[str, Any] | None) -> "JobOptions":
        """Опции из БД. Старые задачи опций не несут — это поведение по умолчанию.

        Переходы до 27.11.2026 своей галочки не имели и выключались вместе с
        «Движением камеры». У задачи тех времён поля `transitions` нет, и
        читается оно как тогдашняя камера: перерендер обязан дать тот же ролик.
        """
        raw = dict(raw or {})
        raw.setdefault("transitions", raw.get("camera", True))
        return cls.model_validate(raw)

    def remembered(self) -> dict[str, bool]:
        """Переключатели, которые запоминаются между роликами (`users.settings`)."""
        return {name: bool(getattr(self, name)) for name in REMEMBERED}


# Что из галочек помнится между роликами — рядом с цветом выделения. Нарезки
# здесь нет намеренно: «без нарезки» выбирают под конкретное видео на первом
# шаге, и запомненный отказ молча оставлял бы паузы в роликах, присланных в чат.
REMEMBERED: tuple[str, ...] = (
    "sfx", "broll", "camera",
    "studio", "tracking", "transitions", "visuals", "title", "progress", "grade", "smooth",
)


def available_switches(cfg: Any) -> list[str]:
    """Переключатели, которые на ЭТОМ сервере что-то меняют, в порядке `REMEMBERED`.

    Приложение показывает только их. Галочка умеет лишь выключить включённое
    (`job_settings`), поэтому там, где сервер приём уже выключил, она не меняла
    бы ничего: на проде `OUTPUT_FPS=0`, и «60 кадров» там включить нечем.
    Галочка, которая ничего не меняет, хуже отсутствующей.
    """
    drawn = cfg.render_backend != "ass"  # быстрый путь графику и переходы не рисует
    works = {
        "sfx": cfg.audio_post_enabled,
        "broll": cfg.broll_enabled,
        "camera": True,
        "studio": cfg.audio_post_enabled
        and (cfg.voice_studio or cfg.voice_denoise_db > 0 or cfg.voice_highpass_hz > 0),
        "tracking": cfg.crop_follow,
        "transitions": cfg.transitions_enabled and drawn,
        "visuals": cfg.visuals_enabled and drawn,
        # Текст титра сочиняет модель (`director.make_metadata`): без неё его нет.
        "title": cfg.llm_ready,
        "progress": cfg.progress_bar and drawn,
        "grade": True,
        "smooth": cfg.output_fps > 0,
    }
    return [name for name in REMEMBERED if works[name]]


def job_settings(cfg: Any, options: JobOptions) -> Any:
    """Настройки сервера, суженные галочками одного ролика.

    Галочка умеет только ВЫКЛЮЧИТЬ то, что на сервере включено: включить
    выключенное владельцем (нет памяти под 60 кадров, графика снята) ролик не
    вправе. Поэтому здесь нет ни одного присваивания `True`.

    Возвращается копия: настройки — общий объект процесса, и правка на месте
    выключила бы графику у всех следующих роликов.
    """
    off: dict[str, Any] = {}
    if not options.studio:
        # Голос как записан: остаётся только выравнивание громкости.
        off.update(voice_studio=False, voice_highpass_hz=0.0, voice_denoise_db=0.0)
    if not options.tracking:
        off["crop_follow"] = False
    if not options.transitions:
        off["transitions_enabled"] = False
    if not options.visuals:
        off["visuals_enabled"] = False
    if not options.progress:
        off["progress_bar"] = False
    if not options.smooth and cfg.output_fps > 0:
        # Частота как у исходника (`pipeline/fps.py`, ноль). Не «ровно 30»:
        # съёмку в 25 кадров перевод в 30 заставил бы дёргаться повторами.
        # Телефон снимает в 30 — рендер рисует вдвое меньше кадров.
        off["output_fps"] = 0
    return cfg.model_copy(update=off) if off else cfg


def apply_grade(preset: Preset, grade: bool) -> Preset:
    """Пресет без цветокоррекции, если человек её выключил. Исходный не трогается."""
    if grade or not preset.grade.enabled:
        return preset
    clone = preset.model_copy(deep=True)
    clone.grade.enabled = False
    return clone


def apply_accent(preset: Preset, accent: str) -> Preset:
    """Пресет с перекрашенным выделенным словом. Исходный объект не трогается.

    Крик красится тем же цветом: выбранный цвет — это «цвет выделения» целиком,
    и второй, чужой ему цвет на пике ролика выглядит как ошибка выбора.
    """
    color = ACCENTS.get(accent, ACCENTS["theme"])[1]
    if color is None:
        return preset
    clone = preset.model_copy(deep=True)
    clone.colors.accent = color
    clone.colors.shout = color
    return clone


def apply_font(preset: Preset, font: str) -> Preset:
    """Пресет с другим шрифтом субтитра. Исходный объект не трогается.

    Кегли пересчитываются, и это главное здесь. Кегль задан в пикселях, а глаз
    меряет высоту прописных: у Oswald SemiBold она 0.810 em против 0.700 у
    Montserrat, то есть те же 88 px дали бы букву на 16 % крупнее задуманного
    темой. Нормируем по `cap_height` из ВЫГРУЖЕННЫХ метрик — тех самых, что
    сверены с Chromium, — а не по числу, переписанному руками в каталог:
    второй источник правды тут разошёлся бы молча.

    Пересчитываются ровно три кегля: основной, тихого регистра и титра. Это те
    же поля, что `Preset.scaled` округляет ВНИЗ, и по той же причине — кегль
    решает перенос строк, и округление вверх теснит строку относительно
    бюджета.

    Чего НЕ трогаем: `letter_spacing` задан в em и едет за кеглем сам, а
    `geometry.min_word_scale` — свойство темы, выведенное под ширины
    Montserrat (см. его докстроку).
    """
    choice = FONTS.get(font, FONTS["theme"])[1]
    if choice is None:
        return preset

    from src.core.geometry import load_metrics

    was, now = load_metrics(preset.font.file or ""), load_metrics(choice.file)
    ratio = 1.0
    if was.cap_height and now.cap_height:
        ratio = (was.cap_height / was.units_per_em) / (now.cap_height / now.units_per_em)

    clone = preset.model_copy(deep=True)
    # Выбранный шрифт — это «вся надпись этим шрифтом». Второй шрифт темы
    # (рукописное главное слово) при этом выключается: иначе выбор менял бы
    # только мелкие слова, а самое заметное слово оставалось бы прежним.
    clone.accent_font = None
    clone.font.family = choice.family
    clone.font.file = choice.file
    clone.font.weight = choice.weight
    # Поправка к пробелу подобрана под шрифт темы; у выбранного шрифта пробел
    # свой. Широкий (моноширинный — 0.6 em) сужаем до обычных 0.28 em: с
    # пробелом шириной в букву строка читается как отдельные слова.
    own_space = now.advance(" ") / now.units_per_em + clone.font.letter_spacing
    clone.font.word_spacing = round(min(0.0, 0.28 - own_space), 3)
    clone.font.size = max(12, int(preset.font.size * ratio))
    clone.caption.quiet.font_size = max(12, int(preset.caption.quiet.font_size * ratio))
    clone.caption.title_card.font_size = max(
        12, int(preset.caption.title_card.font_size * ratio)
    )
    return clone
