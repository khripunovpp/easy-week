"""Настройки приложения, общие для всех устройств семьи: модели по умолчанию для каждой задачи.

JSON-файл `settings.json` рядом с БД (как `app_state.json` / предпочтения). Пишем атомарно
(временный файл + os.replace) — при сбое посреди записи старый файл остаётся целым.

Задачи (ключи `models`):
- chat     — генерация плана, правки в чате, ответы в обсуждении;
- recipe   — развёрнутый рецепт блюда (открыть/перегенерировать, догенерация для PDF/покупок);
- shopping — нормализация списка покупок (по умолчанию Cloudflare — как было раньше);
- cooking  — единый план готовки;
- prefs    — фоновое извлечение предпочтений из сообщений чата (по умолчанию Cloudflare).
Озвучка шагов (ai/tts.py) в настройки не выведена: провайдер один — OpenRouter Fish Audio.

Карта «задача → какие модели можно выбрать» — `TASK_MODELS`. Не всякая модель годится на всё:
Cloudflare (mistral/llama через json_schema) плохо пишет развёрнутые рецепты и планы готовки —
на этих задачах её не предлагаем; бесплатные модели OpenRouter — на пробу для «служебных»
задач (покупки, предпочтения) и плана. Фронт получает карту через GET /api/settings и строит
выпадашки по ней; PUT с неподходящей моделью → 422; `gates.resolve_key` неподходящую явную
модель заменяет дефолтом задачи (с warning в лог) — это политика, а не фолбэк по сбою.

Здесь только чтение/запись дефолтов и карта; выбор гейта — в `ai/gates.gate_for(model, task)`.
Модуль не импортирует `ai/*` (иначе цикл импортов gates ↔ settings).
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal, get_args

from ..config import settings as config

logger = logging.getLogger("easy_week.settings")

# Ключи моделей = ключи реестра GATES (ai/gates.py). Совпадение проверяет тест.
ModelKey = Literal["deepseek", "gemini", "anthropic", "cloudflare", "openrouter"]
MODEL_KEYS: tuple[str, ...] = get_args(ModelKey)

# Задачи, для которых в настройках задаётся модель по умолчанию.
Task = Literal["chat", "recipe", "shopping", "cooking", "prefs"]
TASKS: tuple[str, ...] = get_args(Task)

# «Большие» модели — годятся на всё.
_FULL: tuple[str, ...] = ("deepseek", "gemini", "anthropic")
# Дешёвые/бесплатные — только там, где хватает короткого структурированного ответа.
_CHEAP: tuple[str, ...] = ("cloudflare", "openrouter")

# Карта: задача → модели, которые можно для неё выбрать (порядок = порядок в выпадашках).
TASK_MODELS: dict[str, tuple[str, ...]] = {
    # План: у Cloudflare свой пайплайн меню→спеки→валидатор; OpenRouter — одним запросом (проба).
    "chat": _FULL + _CHEAP,
    # Развёрнутый рецепт и план готовки — длинный связный JSON: дешёвые модели тут плохи.
    "recipe": _FULL,
    "cooking": _FULL,
    # Нормализация покупок — короткий JSON по строгой форме: подходят все.
    "shopping": _FULL + _CHEAP,
    # Извлечение предпочтений — крошечный фоновый вызов на каждое сообщение: только дешёвые
    # и DeepSeek/Gemini (Claude — дорогой и лимитированный, сюда не предлагаем).
    "prefs": ("cloudflare", "openrouter", "deepseek", "gemini"),
}

# Дефолт задачи, если модель из .env (RECIPE_MODEL_DEFAULT) для неё не годится.
_FALLBACK_FULL = "deepseek"


def allowed(task: str, key: str) -> bool:
    """Можно ли выбрать модель key для задачи task (неизвестная задача → как chat)."""
    return key in TASK_MODELS.get(task, TASK_MODELS["chat"])


def _file() -> Path:
    return Path(config.db_path).parent / "settings.json"


def builtin_defaults() -> dict[str, str]:
    """Дефолты, пока настройки не сохранены: рецептные задачи — модель из .env
    (RECIPE_MODEL_DEFAULT, если она годится для задачи), покупки и предпочтения — Cloudflare."""
    base = config.recipe_model_default if config.recipe_model_default in MODEL_KEYS else _FALLBACK_FULL
    out = {"shopping": "cloudflare", "prefs": "cloudflare"}
    for task in ("chat", "recipe", "cooking"):
        out[task] = base if allowed(task, base) else _FALLBACK_FULL
    return out


def _read() -> dict | None:
    """Содержимое файла или None (нет файла / битый JSON)."""
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — битый файл → как будто не сохраняли
        logger.warning("settings.json не читается, берём дефолты: %s", str(exc)[:150])
        return None


def is_initialized() -> bool:
    """Сохранялись ли настройки хоть раз (фронт по этому флагу разово мигрирует localStorage)."""
    return _read() is not None


def get_models() -> dict[str, str]:
    """Модели по умолчанию для всех задач. Неизвестные/пустые/неподходящие задаче значения →
    встроенный дефолт (напр. сохранённый раньше Cloudflare для рецептов)."""
    out = builtin_defaults()
    stored = (_read() or {}).get("models") or {}
    if isinstance(stored, dict):
        for task in TASKS:
            val = str(stored.get(task) or "").lower()
            if val in MODEL_KEYS and allowed(task, val):
                out[task] = val
    return out


def default_model(task: str) -> str:
    """Модель по умолчанию для задачи (неизвестная задача → chat)."""
    models = get_models()
    return models.get(task) or models["chat"]


def set_models(models: dict[str, str]) -> dict[str, str]:
    """Сохранить модели по умолчанию (атомарно). Пары «задача → неподходящая модель»
    молча пропускаются (роутер валидирует их раньше и отдаёт 422). Возвращает итог."""
    merged = {
        **get_models(),
        **{t: m for t, m in models.items() if t in TASKS and m in MODEL_KEYS and allowed(t, m)},
    }
    data = {**(_read() or {}), "models": merged}
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    # Временный файл в том же каталоге → os.replace атомарен (одна ФС).
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=f.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, f)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    logger.info("settings: модели по умолчанию %s", merged)
    return merged
