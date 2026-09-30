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

from ..services import model_catalog
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


def resolve_ref(model: str | None, task: str = "chat") -> tuple[str, str]:
    """(провайдер, конкретная модель или "") для запроса.

    model — «провайдер» или «провайдер:id» (services/model_catalog). Пусто/неизвестно → ссылка
    задачи из настроек. Провайдер не годится для задачи → тоже дефолт задачи (warning).
    Голый провайдер (чат/страница выбрали «Claude») → модель, выбранная для этой задачи в
    настройках, если там тот же провайдер; иначе модель провайдера по умолчанию (.env)."""
    key, mid = model_catalog.split_ref(model)
    default_key, default_mid = model_catalog.split_ref(app_settings.default_ref(task))
    if key in GATES:
        if not app_settings.allowed(task, key):
            logger.warning(
                "модель %s не годится для задачи %s → дефолт задачи %s", key, task, default_key
            )
            return default_key, default_mid
        if mid and not model_catalog.is_known(key, mid):
            mid = ""
        if not mid and key == default_key:
            mid = default_mid
        return key, mid
    return default_key, default_mid


def resolve_key(model: str | None, task: str = "chat") -> str:
    """Реальный ключ провайдера: явно выбранный (если известен и годится для задачи) или
    дефолт задачи из настроек."""
    return resolve_ref(model, task)[0]


def gate_for(model: str | None, task: str = "chat") -> ModelGate:
    """Гейт по ссылке на модель. Пусто/неизвестно/не для этой задачи → модель по умолчанию для
    task (chat — план/правки/обсуждение, recipe — рецепт, shopping — покупки, cooking — готовка,
    prefs — предпочтения, summary — сводка). Конкретная модель — копией гейта (with_model)."""
    key, mid = resolve_ref(model, task)
    return GATES[key].with_model(mid or None)


def is_cloudflare(gate) -> bool:
    """Cloudflare-гейт (в т.ч. копия с выбранной моделью) — ему нужны json_schema и своя модель."""
    return gate is cloudflare or getattr(gate, "key", "") == "cloudflare"


def cf_main(gate) -> str:
    """Главная модель Cloudflare для этого гейта (выбор в настройках или CF_MODEL_JUDGE)."""
    from ..config import settings

    return getattr(gate, "main_model", None) or settings.cf_model_judge


def cf_menu(gate) -> str:
    from ..config import settings

    return getattr(gate, "menu_model", None) or settings.cf_model_menu


__all__ = [
    "AIError", "GATES", "anthropic", "cf_main", "cf_menu", "cloudflare", "deepseek", "gate_for",
    "gemini", "is_cloudflare", "openrouter", "resolve_key", "resolve_ref",
]
