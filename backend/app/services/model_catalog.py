"""Каталог конкретных моделей у каждого провайдера — для выбора в настройках (группы по
провайдерам, внутри — модели).

Значение настройки задачи — «ссылка на модель»: `провайдер` (модель провайдера по умолчанию из
.env) или `провайдер:id` (конкретная модель; id OpenRouter сам содержит «:», поэтому режем по
ПЕРВОМУ двоеточию). Ключ провайдера (deepseek | gemini | anthropic | cloudflare | openrouter)
остаётся ключом везде, где он был: варианты рецептов, оценки, лимиты.

Список — курируемый: только модели, доступные нашим ключам и совместимые с тем, как гейты их
зовут (проверено 2026-09-30: DeepSeek /models, Gemini /models, Anthropic /v1/models). Модель
провайдера по умолчанию из .env добавляется в начало, даже если её здесь нет.
Модуль не импортирует ai/* (settings ↔ gates без цикла).
"""

from ..config import settings

# id → (подпись, пометка). Порядок — порядок в выпадашке.
_CATALOG: dict[str, list[tuple[str, str, str]]] = {
    "deepseek": [
        ("deepseek-chat", "DeepSeek Chat", "быстрая, дешёвая"),
        ("deepseek-flash", "DeepSeek Flash", "размышление выключаем"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro", "сильнее, медленнее"),
    ],
    "gemini": [
        ("gemini-flash-latest", "Gemini Flash", "алиас свежей flash"),
        ("gemini-flash-lite-latest", "Gemini Flash-Lite", "быстрее и дешевле"),
    ],
    "anthropic": [
        # Opus/Fable не предлагаем (дорого); Sonnet 5.5 — самый дешёвый актуальный Sonnet.
        ("claude-haiku-4-5", "Claude Haiku 4.5", "дешёвая, $1/$5"),
        ("claude-sonnet-5-5", "Claude Sonnet 5.5", "сильнее, $2/$10"),
    ],
    "cloudflare": [
        ("@cf/mistralai/mistral-small-3.1-24b-instruct", "Mistral Small 3.1 24B", "бесплатно"),
    ],
    "openrouter": [
        ("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super", "бесплатно"),
        ("google/gemma-4-31b-it:free", "Gemma 4 31B", "бесплатно, часто перегружена"),
        ("qwen/qwen3.8-27b:free", "Qwen 3.8 27B", "бесплатно, часто перегружена"),
    ],
}


def default_id(key: str) -> str:
    """Модель провайдера по умолчанию (.env). У Cloudflare — главная модель (не спеки)."""
    return {
        "deepseek": settings.deepseek_model,
        "gemini": settings.gemini_model,
        "anthropic": settings.anthropic_model,
        "cloudflare": settings.cf_model_judge,
        "openrouter": settings.openrouter_model,
    }.get(key, "")


def models_for(key: str) -> list[dict[str, str]]:
    """Модели провайдера для выпадашки: [{id, label, note}], модель по умолчанию — первой."""
    items = [{"id": i, "label": lab, "note": note} for i, lab, note in _CATALOG.get(key, [])]
    dflt = default_id(key)
    if dflt and not any(x["id"] == dflt for x in items):
        items.insert(0, {"id": dflt, "label": dflt.split("/")[-1], "note": "по умолчанию (.env)"})
    return items


def is_known(key: str, model_id: str) -> bool:
    return any(x["id"] == model_id for x in models_for(key))


def split_ref(ref: str | None) -> tuple[str, str]:
    """«провайдер» | «провайдер:id» → (провайдер, id или "")."""
    ref = (ref or "").strip()
    key, _, mid = ref.partition(":")
    return key.lower(), mid.strip()


def make_ref(key: str, model_id: str = "") -> str:
    """Нормальная форма ссылки: модель по умолчанию — без id (переживёт смену .env)."""
    return key if not model_id or model_id == default_id(key) else f"{key}:{model_id}"


def catalog() -> dict[str, list[dict[str, str]]]:
    return {k: models_for(k) for k in _CATALOG}
