"""Генерация картинок через OpenAI Images API. Написана, но НЕ подключена.

⚠ `images.generate` про этот модуль не знает, в конфиге нет ни ключа OpenAI, ни
значения `image_provider="openai"`. Пока владелец явно не разрешит, ни один
запрос к OpenAI уйти не должен — и не может: функцию нужно вызвать руками и
передать ей ключ. Шов включения — в конце шапки.

Зачем она вообще, если картинки уже рисуют fal, WaveSpeed и Gemini. Ради одного
умения, которого у них нет: ПРОЗРАЧНЫЙ ФОН. Стикер — это вырезанный предмет; у
диффузии без альфа-канала фон пришлось бы вырезать отдельной моделью, а здесь
он приходит прозрачным сразу (`background: "transparent"`).

Форма та же, что у соседей в `images.py`: клиент httpx приходит снаружи, сбой
любого рода — `None`, а не исключение (картинка необязательна, ролик из-за неё
не падает), и повтора нет — каждый запрос платный.

Диалект сверен с документацией OpenAI 17-18.11.2026 (developers.openai.com,
справочник Images и руководство по генерации; ключ не нужен, api.openai.com не
трогался):

- `POST {base}/images/generations`, заголовок `Authorization: Bearer <ключ>`;
- тело: `model`, `prompt` (до 32000 знаков), `n`, `size` (`1024x1024`,
  `1536x1024`, `1024x1536` или `auto`), `quality` (`low` | `medium` | `high` |
  `auto`, у моделей 2.5 и 2.6 ещё `xhigh` и `max`), `output_format` (`png` |
  `jpeg` | `webp`), `background` (`transparent` | `opaque` | `auto`);
- прозрачный фон требует `output_format` png или webp;
- `response_format` модели GPT Image НЕ принимают и всегда отдают картинку
  base64 в `data[0].b64_json`;
- расход — в `usage` ответа (`input_tokens`, `output_tokens`), цена выхода
  $30 за 1M токенов картинки;
- сработавшая модерация — ошибка с `error.code = "moderation_blocked"`;
- сложный промпт может считаться до двух минут.

Чего документация НЕ говорит — это допущения, проверить при включении:

- прозрачный фон прямо обещан для `gpt-image-2.5-sunburst` и
  `gpt-image-2.5-flare` (у `gpt-image-2` — «в превью»); про модели 2.6 страница
  молчит. Поэтому умолчание — 2.5, а не новейшая;
- сколько токенов (то есть денег) стоит картинка у моделей 2.5: таблицы нет,
  только калькулятор. Ориентир из руководства — прежние модели: квадрат стоил
  272 / 1056 / 4160 выходных токенов на low / medium / high, а для gpt-image-2
  названы $0.006 / $0.053 / $0.211 за квадрат. То есть от цента до двадцати.

ШОВ ВКЛЮЧЕНИЯ (делает человек, когда владелец разрешит тратить деньги):

- `src/core/config.py`: `"openai"` в `Literal` поля `image_provider`, поле
  `openai_api_key` (общее с LLM-провайдером), при желании `openai_image_size`
  и `openai_image_quality`;
- `src/llm/images.py`: строка `"openai": DEFAULT_MODEL` в
  `PROVIDER_IMAGE_MODELS`, `"openai": cfg.openai_api_key` в `image_key`, размер
  в `size_tag`, ветка `if cfg.image_provider == "openai"` в `generate`,
  вызывающая `generate_openai`. Референсов эта ручка не принимает (они идут
  через `/images/edits`), так что `uses_refs` остаётся как есть;
- стикеру нужен `transparent=True`, а у `images.generate` такого параметра нет:
  генерация стикера — отдельный вызов `generate_openai` из расстановщика
  графики, и результат надо обвести тем же кодом, что и набор
  (`tools/make_stickers.py: build_sticker`);
- `tests/test_openai_provider.py`: тест «провайдер не подключён» удаляется
  осознанно, тем же коммитом.
"""

from __future__ import annotations

import base64
import logging
import re

import httpx

from src.llm.openai_provider import OPENAI_BASE_URL

log = logging.getLogger(__name__)

# ПОДТВЕРДИТЬ ПРИ ВКЛЮЧЕНИИ: имя взято со страницы моделей 18.11.2026.
# `flare` — «быстрая повседневная генерация»; прозрачный фон у неё обещан прямо.
DEFAULT_MODEL = "gpt-image-2.5-flare"
DEFAULT_SIZE = "1024x1024"
# Черновое качество: стикер в кадре занимает пару сотен пикселей, а каждая
# следующая ступень качества дороже предыдущей в разы. Поднимается параметром,
# не правкой кода.
DEFAULT_QUALITY = "low"
# «Сложный промпт может считаться до двух минут» — ждём с запасом.
OPENAI_IMAGE_TIMEOUT_S = 180.0

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SIZE_RE = re.compile(r"\d{2,5}x\d{2,5}|auto")


async def generate_openai(
    client: httpx.AsyncClient,
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    size: str = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
    transparent: bool = False,
    base_url: str = OPENAI_BASE_URL,
    timeout: float = OPENAI_IMAGE_TIMEOUT_S,
) -> bytes | None:
    """Картинка по промпту: байты PNG или `None`, если её нет.

    `transparent=True` — предмет на прозрачном фоне, для стикеров. Иначе фон
    просится непрозрачным явно: значение по умолчанию у API — `auto`, то есть
    модель решала бы сама, и вставка в ролик иногда приходила бы с дырой.

    Запрос один и повтора нет: и отказ модерации, и исчерпанный баланс повтором
    не лечатся, а платить за второй раз пришлось бы.
    """
    if not api_key or not model:
        return None
    if not _SIZE_RE.fullmatch(size):
        # Проверяем до запроса: опечатка в размере не должна стоить обращения.
        log.warning("OpenAI: размер %r не похож на ШИРИНАxВЫСОТА", size)
        return None

    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": quality,
        # PNG — единственный из форматов, который и без потерь, и с альфой, и
        # читается всем остальным пайплайном без перекодирования.
        "output_format": "png",
        "background": "transparent" if transparent else "opaque",
        # `response_format` не шлём: модели GPT Image его не принимают и всегда
        # отвечают base64.
    }
    try:
        r = await client.post(
            f"{base_url.rstrip('/')}/images/generations",
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json()
        items = payload.get("data") or []
        encoded = items[0].get("b64_json") if items else None
        if not isinstance(encoded, str) or not encoded:
            log.warning("OpenAI не вернул картинку: %s", r.text[:200])
            return None
        image = base64.b64decode(encoded, validate=True)
    except httpx.HTTPStatusError as e:
        # Тело ошибки называет причину: модерация, баланс, неверная модель.
        log.warning(
            "OpenAI отказал (HTTP %s) на %r: %s",
            e.response.status_code, prompt[:40], e.response.text[:200],
        )
        return None
    except (httpx.HTTPError, ValueError, AttributeError, TypeError, LookupError) as e:
        # ValueError — и не-JSON в теле, и битый base64 (binascii.Error — его подвид).
        # LookupError — `data` пришло не списком: объект даёт KeyError, а не IndexError.
        log.warning("OpenAI не сгенерировал %r: %s", prompt[:40], e)
        return None

    if not image.startswith(PNG_MAGIC):
        log.warning("OpenAI вернул не PNG (%r…) на %r", image[:8], prompt[:40])
        return None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        # Единственное место, где виден расход: цена картинки считается в
        # токенах, и без этой строки её негде было бы взять.
        log.info(
            "OpenAI картинка [%s %s %s]: вход %s ток., выход %s ток.",
            model, size, quality, usage.get("input_tokens"), usage.get("output_tokens"),
        )
    return image
