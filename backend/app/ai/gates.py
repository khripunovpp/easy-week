"""Реестр гейтов моделей и выбор по ключу — единственная точка выбора провайдера.

Ключ приходит с фронта (`recipe_model`): deepseek | gemini | cloudflare | anthropic.
Пустой/неизвестный ключ → модель по умолчанию для ЗАДАЧИ из общих настроек
(`services/settings.py`: chat | recipe | shopping | cooking).
Фолбэков нет: `gate_for` просто отдаёт нужный гейт, а недоступность модели всплывает как `AIError`.
"""

from ..services import settings as app_settings
from .anthropic import AnthropicGate
from .base import AIError, ModelGate
from .cloudflare import CloudflareGate
from .deepseek import DeepSeekGate
from .gemini import GeminiGate

deepseek = DeepSeekGate()
cloudflare = CloudflareGate()
gemini = GeminiGate()
anthropic = AnthropicGate()

GATES: dict[str, ModelGate] = {g.key: g for g in (deepseek, gemini, cloudflare, anthropic)}


def resolve_key(model: str | None, task: str = "chat") -> str:
    """Реальный ключ модели: явно выбранный (если известен) или дефолт задачи из настроек."""
    key = (model or "").strip().lower()
    if key in GATES:
        return key
    return app_settings.default_model(task)


def gate_for(model: str | None, task: str = "chat") -> ModelGate:
    """Гейт по ключу модели. Пусто/неизвестно → модель по умолчанию для задачи task
    (chat — план/правки/обсуждение, recipe — рецепт, shopping — покупки, cooking — готовка)."""
    return GATES[resolve_key(model, task)]


__all__ = [
    "AIError", "GATES", "anthropic", "cloudflare", "deepseek", "gate_for", "gemini", "resolve_key",
]
