"""Реестр гейтов моделей и выбор по ключу — единственная точка выбора провайдера.

Ключ приходит с фронта (`recipe_model`): deepseek | gemini | cloudflare | anthropic.
Фолбэков нет: `gate_for` просто отдаёт нужный гейт, а недоступность модели всплывает как `AIError`.
"""

from ..config import settings
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


def gate_for(model: str | None) -> ModelGate:
    """Гейт по ключу модели рецептов. Неизвестный ключ → дефолт из настроек."""
    key = (model or settings.recipe_model_default).lower()
    return GATES.get(key) or GATES[settings.recipe_model_default]


__all__ = ["AIError", "GATES", "anthropic", "cloudflare", "deepseek", "gate_for", "gemini"]
