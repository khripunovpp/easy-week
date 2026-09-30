"""Реестр гейтов моделей и выбор по ключу — единственная точка выбора провайдера.

Ключ приходит с фронта (`recipe_model`): deepseek | gemini | cloudflare | anthropic | openrouter.
Пустой/неизвестный ключ → модель по умолчанию для ЗАДАЧИ из общих настроек
(`services/settings.py`: chat | recipe | shopping | cooking | prefs).
Карта «какие модели годятся для задачи» — там же (`TASK_MODELS`): явная модель, не подходящая
задаче (старый клиент, внутренний шаг вроде правки рецепта моделью чата), заменяется дефолтом
задачи с warning в лог. Это политика выбора, а не фолбэк: сбой модели по-прежнему всплывает
как `AIError`, на другую модель не переключаемся.
"""

import logging

from ..services import settings as app_settings
from .anthropic import AnthropicGate
from .base import AIError, ModelGate
from .cloudflare import CloudflareGate
from .deepseek import DeepSeekGate
from .gemini import GeminiGate
from .openrouter import OpenRouterGate

logger = logging.getLogger("easy_week.ai.gates")

deepseek = DeepSeekGate()
cloudflare = CloudflareGate()
gemini = GeminiGate()
anthropic = AnthropicGate()
openrouter = OpenRouterGate()

GATES: dict[str, ModelGate] = {
    g.key: g for g in (deepseek, gemini, cloudflare, anthropic, openrouter)
}


def resolve_key(model: str | None, task: str = "chat") -> str:
    """Реальный ключ модели: явно выбранный (если известен и годится для задачи) или дефолт
    задачи из настроек."""
    key = (model or "").strip().lower()
    if key in GATES:
        if app_settings.allowed(task, key):
            return key
        default = app_settings.default_model(task)
        logger.warning("модель %s не годится для задачи %s → дефолт задачи %s", key, task, default)
        return default
    return app_settings.default_model(task)


def gate_for(model: str | None, task: str = "chat") -> ModelGate:
    """Гейт по ключу модели. Пусто/неизвестно/не для этой задачи → модель по умолчанию для task
    (chat — план/правки/обсуждение, recipe — рецепт, shopping — покупки, cooking — готовка,
    prefs — извлечение предпочтений)."""
    return GATES[resolve_key(model, task)]


__all__ = [
    "AIError", "GATES", "anthropic", "cloudflare", "deepseek", "gate_for", "gemini",
    "openrouter", "resolve_key",
]
