"""Настройки приложения, общие для всех устройств семьи: модели по умолчанию для каждой задачи.

JSON-файл `settings.json` рядом с БД (как `app_state.json` / предпочтения). Пишем атомарно
(временный файл + os.replace) — при сбое посреди записи старый файл остаётся целым.

Задачи (ключи `models`):
- chat     — генерация плана, правки в чате, ответы в обсуждении;
- recipe   — развёрнутый рецепт блюда (открыть/перегенерировать, догенерация для PDF/покупок);
- shopping — нормализация списка покупок (по умолчанию Cloudflare — как было раньше);
- cooking  — единый план готовки;
- prefs    — фоновое извлечение предпочтений из сообщений чата (по умолчанию Cloudflare);
- summary  — фоновая сводка беседы после реплик пользователя (по умолчанию Cloudflare).
Озвучка шагов (ai/tts.py) в настройки не выведена: провайдер один — OpenRouter Fish Audio.

Карта «задача → какие модели можно выбрать» — `TASK_MODELS`. Не всякая модель годится на всё:
Cloudflare (mistral/llama через json_schema) плохо пишет развёрнутые рецепты и планы готовки —
на этих задачах её не предлагаем; бесплатные модели OpenRouter — на пробу для «служебных»
задач (предпочтения, сводка) и плана; покупки им не доверяем (путали количества). Фронт получает карту через GET /api/settings и строит
выпадашки по ней; PUT с неподходящей моделью → 422; `gates.resolve_key` неподходящую явную
модель заменяет дефолтом задачи (с warning в лог) — это политика, а не фолбэк по сбою.

Значение задачи — ссылка на модель (`services/model_catalog`): «провайдер» (модель провайдера
из .env) или «провайдер:id» (конкретная модель из каталога). Карта задач — по провайдеру.

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
from . import model_catalog

logger = logging.getLogger("easy_week.settings")

# Ключи моделей = ключи реестра GATES (ai/gates.py). Совпадение проверяет тест.
ModelKey = Literal["deepseek", "gemini", "anthropic", "cloudflare", "openrouter"]
MODEL_KEYS: tuple[str, ...] = get_args(ModelKey)

# Задачи, для которых в настройках задаётся модель по умолчанию.
Task = Literal["chat", "recipe", "shopping", "cooking", "prefs", "summary", "fix"]
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
    # Нормализация покупок — короткий JSON по строгой форме. Бесплатные модели OpenRouter —
    # нет: склеивали неверно (рис 160 + «рис белый» 100 г, лук 650 → 750 г, яйца в двух
    # группах — 👎 2026-10-09), проба не удалась.
    "shopping": _FULL + ("cloudflare",),
    # Извлечение предпочтений — крошечный фоновый вызов на каждое сообщение: только дешёвые
    # и DeepSeek/Gemini (Claude — дорогой и лимитированный, сюда не предлагаем).
    "prefs": ("cloudflare", "openrouter", "deepseek", "gemini"),
    # Сводка беседы — фоновый вызов после каждой реплики: только дешёвые/бесплатные и
    # DeepSeek/Gemini (Claude — дорогой и лимитированный). По умолчанию Cloudflare: у бесплатных
    # моделей OpenRouter дневной лимит запросов, а сводка — самый частый вызов.
    "summary": ("cloudflare", "openrouter", "deepseek", "gemini"),
    # Точечная правка рецепта («Исправить»: убрать лук) — ответ короткий: только изменённые
    # строки (prompt.FIX_RECIPE_SYSTEM), применяет их код. По умолчанию DeepSeek (дёшево и
    # аккуратно); Cloudflare mistral — нет: на «убери перец» убрал и лук и сдвинул номера шагов
    # (текст шага 6 пропал) — прогон 2026-10-09. OpenRouter — проба.
    "fix": _FULL + ("openrouter",),
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
    out = {"shopping": "cloudflare", "prefs": "cloudflare", "summary": "cloudflare",
           "fix": "deepseek"}
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


def valid_ref(task: str, ref: str) -> str | None:
    """Нормальная ссылка «провайдер[:id]», если она годится для задачи, иначе None."""
    key, mid = model_catalog.split_ref(ref)
    if key not in MODEL_KEYS or not allowed(task, key):
        return None
    if mid and not model_catalog.is_known(key, mid):
        return None
    return model_catalog.make_ref(key, mid)


def get_models() -> dict[str, str]:
    """Ссылки на модели по умолчанию для всех задач. Неизвестные/пустые/неподходящие задаче
    значения → встроенный дефолт (напр. сохранённый раньше Cloudflare для рецептов); модель,
    которой больше нет в каталоге, → модель этого провайдера по умолчанию."""
    out = builtin_defaults()
    stored = (_read() or {}).get("models") or {}
    if isinstance(stored, dict):
        for task in TASKS:
            raw = str(stored.get(task) or "").strip()
            ref = valid_ref(task, raw)
            if ref is None and raw:
                key, _ = model_catalog.split_ref(raw)
                ref = valid_ref(task, key)  # модель пропала из каталога — провайдер остаётся
            if ref:
                out[task] = ref
    return out


def default_ref(task: str) -> str:
    """Ссылка на модель по умолчанию для задачи (неизвестная задача → chat)."""
    models = get_models()
    return models.get(task) or models["chat"]


def default_model(task: str) -> str:
    """Провайдер по умолчанию для задачи (неизвестная задача → chat)."""
    return model_catalog.split_ref(default_ref(task))[0]


def set_models(models: dict[str, str]) -> dict[str, str]:
    """Сохранить модели по умолчанию (атомарно). Пары «задача → неподходящая модель»
    молча пропускаются (роутер валидирует их раньше и отдаёт 422). Возвращает итог."""
    merged = get_models()
    for t, m in models.items():
        ref = valid_ref(t, m) if t in TASKS else None
        if ref:
            merged[t] = ref
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
