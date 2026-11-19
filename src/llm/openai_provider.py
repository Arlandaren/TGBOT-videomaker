"""Провайдер OpenAI (Chat Completions). Написан, но НЕ подключён.

⚠ Модуль никуда не зарегистрирован: его нет ни в `providers.PROVIDERS`, ни в
конфиге, ни в таблице цен. Пока владелец явно не разрешит, ни один запрос к
OpenAI уйти не должен — и не может: класс нужно сначала импортировать руками и
дать ему ключ. Что именно поменять при включении — в конце этой шапки.

Зачем отдельный класс, если в реестре уже есть строка `"openai"`. Она ведёт на
`OpenAICompatProvider`, а тот говорит на диалекте DeepSeek, и против настоящего
OpenAI не работает по четырём причинам сразу:

1. шлёт `thinking: {"type": "disabled"}` — параметра с таким именем у OpenAI
   нет, размышлениями там управляет `reasoning_effort`;
2. шлёт `max_tokens` — параметр устарел в пользу `max_completion_tokens` и,
   по справочнику, несовместим с моделями o-серии;
3. просит `response_format: json_object` и кладёт схему в промпт, хотя OpenAI
   умеет принуждать к схеме (`json_schema`);
4. шлёт `temperature: 0.4`.

Транспорт, заголовки и разбор ошибок при этом те же, поэтому класс построен на
`OpenAICompatProvider`: от него взяты `_ensure`, `_post`, `aclose`. Тело запроса
и разбор ответа — свои и собраны целиком здесь, а не правкой чужого тела:
появись в базовом классе ещё один ключ под DeepSeek, сюда он не протечёт.

Диалект сверен с документацией OpenAI 17-18.11.2026 (developers.openai.com,
справочник Chat Completions и руководства; ключ не нужен, api.openai.com не
трогался):

- `POST {base}/chat/completions`, заголовок `Authorization: Bearer <ключ>`;
- `max_completion_tokens` — потолок выхода ВМЕСТЕ с размышлениями;
- `response_format: {"type": "json_schema", "json_schema": {name, strict,
  schema}}`; `strict` по умолчанию false; строгий режим требует, чтобы все поля
  были в `required`, а у каждого объекта стояло `additionalProperties: false`;
  неподходящая схема — ошибка 400 с кодом `invalid_json_schema`;
- `reasoning_effort`: none | minimal | low | medium | high | xhigh | max, но
  «не каждая модель принимает каждое значение». На 18.11.2026: gpt-6-luna и
  gpt-6.1-terra принимают none (minimal у них не значится) и по умолчанию
  думают на medium; gpt-6.2-sol и gpt-6.2-astra на none отвечают 400;
- роль системного сообщения — `developer` («для o1 и новее сообщения developer
  заменяют system»);
- картинка — часть `{"type": "image_url", "image_url": {"url": "data:..."}}`;
- расход: `usage.prompt_tokens`, `usage.completion_tokens`,
  `usage.prompt_tokens_details.cached_tokens` и `.cache_write_tokens`;
- `finish_reason`: stop | length | content_filter | tool_calls; отказ модели
  приходит строкой в `message.refusal`.

Чего документация НЕ говорит прямо — это допущения, они помечены в коде словом
ДОПУЩЕНИЕ и должны быть проверены первым живым вызовом при включении:

- что `completion_tokens` уже включает токены размышлений (справочник Chat
  Completions молчит; пример в руководстве по размышлениям показывает это для
  Responses API);
- что `prompt_tokens` включает и кешированные, и записанные в кеш токены
  (формула дана для `input_tokens` Responses API);
- какими словами модель отказывается от неподдерживаемого `reasoning_effort`;
- что запрос, отклонённый с 400, не тарифицируется.

ШОВ ВКЛЮЧЕНИЯ (делает человек, когда владелец разрешит тратить деньги):

- `src/llm/providers.py`, `make_provider`: перед `cls = PROVIDERS[name]` ветка
  `if name == "openai":` с ЛЕНИВЫМ импортом `OpenAIProvider` внутри функции и
  вызовом `OpenAIProvider(api_key, base_url, timeout, thinking=thinking)`.
  Импорт наверху модуля нельзя: этот файл сам импортирует `providers`, выйдет
  круг. В `PRICES` — строки из `OPENAI_PRICES`;
- `src/core/config.py`: `"openai"` в `Literal` поля `llm_provider`, поля
  `openai_api_key` и `openai_base_url` (= `OPENAI_BASE_URL`), строки в
  `llm_api_key` и `llm_base_url`, блок `PROVIDER_MODELS["openai"]` =
  `OPENAI_MODELS`;
- `tests/test_openai_provider.py`: тест «провайдер не подключён» удаляется
  осознанно, тем же коммитом.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any, Literal, Sequence

from src.llm.providers import OpenAICompatProvider, ProviderError, ProviderResult

log = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"

# Модели по тирам. ПОДТВЕРДИТЬ ПРИ ВКЛЮЧЕНИИ: имена взяты со страницы моделей
# 18.11.2026, а линейка у OpenAI меняется раз в несколько месяцев.
#
# `smart` — не старшая модель линейки, и это тот же выбор, что сделан для fal
# (см. PROVIDER_MODELS в config.py): пока эвал-сет не показал, что дорогая
# модель даёт ЛУЧШИЙ монтаж, платить за неё не за что. Старшая включается одной
# строкой: LLM_TIER_SMART=gpt-6.2-sol.
OPENAI_MODELS: dict[str, str] = {
    "cheap": "gpt-6-luna",
    "smart": "gpt-6.1-terra",
    "vision": "gpt-6.1-terra",
}

# $/1M токенов: (вход-промах, вход-кеш, выход) — формат `providers.PRICES`.
# Страница цен, стандартный тариф, 18.11.2026. ПОДТВЕРДИТЬ ПРИ ВКЛЮЧЕНИИ.
# Запись в кеш у этих моделей стоит 1.25× входа (окно 30 минут) — ровно тот
# множитель, который `Usage.cost_usd` и так берёт для `cache_creation_tokens`.
OPENAI_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6.2-astra": (8.00, 0.40, 40.00),
    "gpt-6.2-sol": (2.00, 0.10, 10.00),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
}

# Имя схемы в `response_format`: обязательное поле, буквы-цифры-подчёркивания.
SCHEMA_NAME = "answer"

# С чего начинать, когда размышления выключены (`thinking=False`), и куда
# отступать, если модель такое усилие не принимает. Список моделей зашивать
# нельзя — он протухнет к следующему релизу (тот же урок, что у Gemini в
# providers.py), поэтому узнаём по факту отказа и запоминаем.
EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low")
EFFORTS_ON_REQUEST = ("low", "medium", "high")

# Ключевые слова JSON Schema, с которыми строгий режим заведомо работает
# (раздел «Supported schemas» руководства по Structured Outputs). Всё, чего в
# списке нет, — `maxLength`, `const`, `allOf`, `$ref` — повод не включать
# строгий режим, а не повод гадать: нестрогий примет любую схему.
_STRICT_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "anyOf",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "format",
    }
)
_STRICT_MAX_DEPTH = 10  # «до 10 уровней вложенности»

_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


def strict_ready(schema: Any) -> bool:
    """Годится ли схема для строгого режима (`strict: true`).

    Строгий режим — единственный, в котором OpenAI ГАРАНТИРУЕТ форму ответа, и
    каждая такая гарантия — это несостоявшийся платный повтор с поправкой. Но
    требования у него жёсткие: корень — объект, у каждого объекта
    `additionalProperties: false` и ВСЕ поля в `required`. Половина наших схем
    их не выполняет намеренно (см. `panel` в DIRECT_SCHEMA: забытое
    необязательное поле не стоит ретрая), поэтому решение принимается по самой
    схеме, а не одним флагом на провайдера.

    Проверка осторожная: сомнение трактуется как «не годится». Ошибиться в эту
    сторону не страшно — нестрогий режим примет схему, а форму всё равно
    проверит `jsonshape.check`.
    """
    if not isinstance(schema, dict) or schema.get("type") != "object" or "anyOf" in schema:
        return False
    return _strict_node(schema, 1)


def _strict_node(node: Any, depth: int) -> bool:
    if not isinstance(node, dict) or depth > _STRICT_MAX_DEPTH:
        return False
    if not set(node) <= _STRICT_KEYWORDS:
        return False
    # Узел без типа («что угодно») строгий режим не описывает: сомнение — «нет».
    if not {"type", "anyOf", "enum"} & set(node):
        return False
    kinds = node.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if "object" in kinds or "properties" in node:
        props = node.get("properties")
        if not isinstance(props, dict) or node.get("additionalProperties") is not False:
            return False
        if set(node.get("required") or ()) != set(props):
            return False
        if not all(_strict_node(sub, depth + 1) for sub in props.values()):
            return False
    if "items" in node and not _strict_node(node["items"], depth + 1):
        return False
    options = node.get("anyOf", [])
    return isinstance(options, list) and all(_strict_node(sub, depth) for sub in options)


def _schema_digest(schema: Any) -> str:
    """Отпечаток схемы: по нему помним, какую из них строгий режим отверг."""
    raw = json.dumps(schema, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def image_mime(data: bytes) -> str:
    """Тип картинки по первым байтам. Кадры ролика — JPEG, но стикер будет PNG."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class OpenAIProvider(OpenAICompatProvider):
    """Настоящий OpenAI: Chat Completions с принуждением к схеме.

    `thinking=False` (умолчание проекта, `LLM_THINKING`) значит «думать как
    можно меньше»: размышления считаются в `max_completion_tokens` и
    оплачиваются по цене выхода, а задачи у нас структурные. Полностью выключить
    их дают не все модели, поэтому усилие подбирается лестницей `EFFORT_LADDER`
    по отказам модели и запоминается на время жизни провайдера.

    `strict` — `"auto"` (решает `strict_ready` по самой схеме), `True` или
    `False`.
    """

    name = "openai"
    supports_schema = True
    supports_images = True
    # Звук в Chat Completions слушают только отдельные аудио-модели; у моделей
    # GPT-6 вход — текст и картинка. Клиент такому провайдеру звук не передаёт.
    supports_audio = False
    auth_scheme = "Bearer"
    #: роль системного сообщения. У o1 и новее `developer` заменяет `system`
    system_role = "developer"

    def __init__(
        self,
        api_key: str,
        base_url: str = OPENAI_BASE_URL,
        timeout: float = 180.0,
        *,
        thinking: bool = False,
        strict: bool | Literal["auto"] = "auto",
    ) -> None:
        super().__init__(api_key, base_url, timeout, thinking=thinking)
        self.strict = strict
        #: модель → наименьшее усилие, которое она приняла
        self._effort_floor: dict[str, str] = {}
        #: отпечатки схем, которые строгий режим отверг
        self._not_strict: set[str] = set()

    # ------------------------------------------------------------------ запрос

    def _response_format(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.strict == "auto":
            strict = strict_ready(schema) and _schema_digest(schema) not in self._not_strict
        else:
            strict = bool(self.strict)
        return {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": strict, "schema": schema},
        }

    def _effort(self, model: str, effort: str | None) -> str | None:
        """Значение `reasoning_effort` или None — «ключ не слать, решит модель»."""
        if not self.thinking:
            return self._effort_floor.get(model, EFFORT_LADDER[0])
        return effort if effort in EFFORTS_ON_REQUEST else None

    def _body(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        images: Sequence[bytes],
        max_tokens: int,
        effort: str | None,
    ) -> dict[str, Any]:
        if images:
            content: Any = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_mime(img)};base64,"
                        + base64.standard_b64encode(img).decode("ascii")
                    },
                }
                for img in images
            ]
            content.append({"type": "text", "text": user})
        else:
            content = user

        body: dict[str, Any] = {
            "model": model,
            # Системное сообщение первым и неизменным: кеш у OpenAI
            # автоматический и держится на совпадении префикса запроса.
            "messages": [
                {"role": self.system_role, "content": system},
                {"role": "user", "content": content},
            ],
            # Потолок выхода ВМЕСТЕ с размышлениями. Прежний `max_tokens`
            # устарел.
            "max_completion_tokens": max_tokens,
        }
        # `temperature` не шлём совсем: документация GPT-6 про него молчит, а
        # умолчание (1) принимает любая модель. Ключа `thinking` у OpenAI нет.
        if schema:
            body["response_format"] = self._response_format(schema)
        level = self._effort(model, effort)
        if level is not None:
            body["reasoning_effort"] = level
        return body

    # ------------------------------------------------------------------ отказы 400

    def _relax(self, body: dict[str, Any], why: str) -> str | None:
        """Ослабляет запрос, отклонённый с 400. Возвращает, что ослабил, или None.

        Спорных настроек две: строгий режим схемы и наименьшее усилие
        размышлений. Обе мы выставляем сами, «по возможности», и обе модель
        вправе не принять. Что именно не понравилось, узнаём по тексту ошибки;
        если он ни о чём не говорит — снимаем сначала строгий режим, потом
        поднимаем усилие.

        ДОПУЩЕНИЕ: слова отказа от `reasoning_effort` в документации не
        приведены (сказано только «возвращает 400»), поэтому на них одних
        полагаться нельзя — отсюда и последний шаг «по умолчанию». Код отказа
        схемы — `invalid_json_schema` — документирован.

        ДОПУЩЕНИЕ: отклонённый с 400 запрос не тарифицируется (токены не
        сгенерированы). На том же стоят повторы у Gemini и fal в providers.py.
        """
        text = why.lower()
        fmt = (body.get("response_format") or {}).get("json_schema") or {}
        current = body.get("reasoning_effort")
        higher: str | None = None
        if not self.thinking and current in EFFORT_LADDER[:-1]:
            higher = EFFORT_LADDER[EFFORT_LADDER.index(current) + 1]

        blames_effort = "reasoning_effort" in text or "reasoning effort" in text
        if higher and blames_effort:
            body["reasoning_effort"] = higher
            return "effort"
        if fmt.get("strict"):
            fmt["strict"] = False
            return "strict"
        if higher:
            body["reasoning_effort"] = higher
            return "effort"
        return None

    async def _send(self, body: dict[str, Any], *, model: str) -> dict[str, Any]:
        strict_dropped = False
        while True:
            try:
                data = await self._post(body)
            except ProviderError as e:
                # Ослабляем только 400: это «запрос не такой», а не «сервис
                # лежит». Лестница конечна — три ступени усилия и один флаг.
                relaxed = self._relax(body, str(e)) if e.status == 400 else None
                if relaxed is None:
                    raise
                strict_dropped = strict_dropped or relaxed == "strict"
                log.info("%s: отказ 400, ослабляю запрос (%s) и повторяю", model, relaxed)
                continue
            # Урок запоминается только после УСПЕХА: 400 бывает и по другой
            # причине, и тогда ослабление ничего не доказало.
            if not self.thinking and "reasoning_effort" in body:
                self._effort_floor[model] = body["reasoning_effort"]
            if strict_dropped:
                schema = body["response_format"]["json_schema"]["schema"]
                self._not_strict.add(_schema_digest(schema))
            return data

    # ------------------------------------------------------------------ вызов

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        images: Sequence[bytes] = (),
        max_tokens: int = 16_000,
        effort: str | None = None,
    ) -> ProviderResult:
        """Один вызов. Пустая схема — свободный текст, `response_format` не шлётся."""
        body = self._body(
            model=model,
            system=system,
            user=user,
            schema=schema,
            images=images,
            max_tokens=max_tokens,
            effort=effort,
        )
        return self._result(await self._send(body, model=model))

    @staticmethod
    def _result(data: dict[str, Any]) -> ProviderResult:
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}

        text = message.get("content")
        text = text if isinstance(text, str) else ""
        stop = str(choice.get("finish_reason") or "")
        refusal = message.get("refusal")
        if not text and refusal:
            # Отказ — не ошибка транспорта: модель ответила и взяла за это
            # деньги. Возвращаем пустой текст с понятной причиной, как делает
            # Anthropic (`stop_reason: refusal`): клиент запишет расход и
            # поднимет LLMBadOutput.
            log.warning("OpenAI: модель отказалась отвечать: %s", str(refusal)[:200])
            stop = "refusal"

        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached = _count(details.get("cached_tokens"))
        written = _count(details.get("cache_write_tokens"))
        return ProviderResult(
            text=text,
            # ДОПУЩЕНИЕ: `prompt_tokens` включает и прочитанное из кеша, и
            # записанное в него — вычитаем, чтобы не посчитать дважды.
            input_tokens=max(0, _count(usage.get("prompt_tokens")) - cached - written),
            # ДОПУЩЕНИЕ: размышления уже внутри `completion_tokens`
            # (`completion_tokens_details.reasoning_tokens` — их расшифровка, а
            # не добавка), поэтому отдельно не прибавляются.
            output_tokens=_count(usage.get("completion_tokens")),
            cache_read_tokens=cached,
            cache_creation_tokens=written,
            stop_reason=stop,
            # Стоимость OpenAI в ответе не сообщает — считает таблица цен.
            billed_usd=None,
        )


def _count(value: Any) -> int:
    """Счётчик токенов из ответа. Мусор вместо числа — ноль, а не падение."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
