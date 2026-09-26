"""Настройки приложения, общие для всех устройств семьи: модели по умолчанию для каждой задачи.

JSON-файл `settings.json` рядом с БД (как `app_state.json` / предпочтения). Пишем атомарно
(временный файл + os.replace) — при сбое посреди записи старый файл остаётся целым.

Задачи (ключи `models`):
- chat     — генерация плана, правки в чате, ответы в обсуждении;
- recipe   — развёрнутый рецепт блюда (открыть/перегенерировать, догенерация для PDF/покупок);
- shopping — нормализация списка покупок (по умолчанию Cloudflare — как было раньше);
- cooking  — единый план готовки.

Здесь только чтение/запись дефолтов; выбор гейта по задаче — в `ai/gates.gate_for(model, task)`.
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
ModelKey = Literal["deepseek", "gemini", "anthropic", "cloudflare"]
MODEL_KEYS: tuple[str, ...] = get_args(ModelKey)

# Задачи, для которых в настройках задаётся модель по умолчанию.
Task = Literal["chat", "recipe", "shopping", "cooking"]
TASKS: tuple[str, ...] = get_args(Task)


def _file() -> Path:
    return Path(config.db_path).parent / "settings.json"


def builtin_defaults() -> dict[str, str]:
    """Дефолты, пока настройки не сохранены: рецептные задачи — модель из .env
    (RECIPE_MODEL_DEFAULT), список покупок — Cloudflare (прежнее поведение)."""
    base = config.recipe_model_default if config.recipe_model_default in MODEL_KEYS else "deepseek"
    return {"chat": base, "recipe": base, "shopping": "cloudflare", "cooking": base}


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
    """Модели по умолчанию для всех задач. Неизвестные/пустые значения → встроенный дефолт."""
    out = builtin_defaults()
    stored = (_read() or {}).get("models") or {}
    if isinstance(stored, dict):
        for task in TASKS:
            val = str(stored.get(task) or "").lower()
            if val in MODEL_KEYS:
                out[task] = val
    return out


def default_model(task: str) -> str:
    """Модель по умолчанию для задачи (неизвестная задача → chat)."""
    models = get_models()
    return models.get(task) or models["chat"]


def set_models(models: dict[str, str]) -> dict[str, str]:
    """Сохранить модели по умолчанию (атомарно). Возвращает итоговые значения."""
    merged = {**get_models(), **{t: m for t, m in models.items() if t in TASKS and m in MODEL_KEYS}}
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
