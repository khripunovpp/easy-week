"""Общие настройки приложения (одни на все устройства семьи): модели по умолчанию по задачам.

GET /api/settings  → {models: {chat, recipe, shopping, cooking}, initialized}
PUT /api/settings  ← {models: {...}} — ключи моделей валидирует схема (Literal ключей GATES).
GET/PUT /api/settings/prices — цены моделей для учёта затрат (USD за 1M токенов).
Хранение — `services/settings.py` (settings.json рядом с БД, атомарная запись).
"""

from fastapi import APIRouter

from ..schemas import ModelDefaults, ModelPrice, PricesBody, SettingsBody, SettingsOut
from ..services import prices
from ..services import settings as app_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _model_names() -> dict[str, str]:
    """Ключ → конкретная модель из конфига. У Gemini алиас (gemini-flash-latest) дополняем
    реальной версией из последнего ответа, если она уже известна."""
    from ..ai import gemini  # локально: gates тянет гейты, а они — observe/настройки
    from ..ai.gates import GATES

    from ..config import settings

    names = {k: g._log_model(g.default_model) for k, g in GATES.items()}
    # Cloudflare — пайплайн из нескольких моделей: главная (mistral: рецепты, покупки, правки),
    # затем быстрая для спек блюд. Показываем все уникальные, главная первой.
    cf = [settings.cf_model_judge, settings.cf_model_menu, settings.cf_model]
    names["cloudflare"] = " + ".join(dict.fromkeys(m.split("/")[-1] for m in cf if m))
    if gemini.resolved_model and gemini.resolved_model != names.get("gemini"):
        names["gemini"] = f"{names.get('gemini', '')} → {gemini.resolved_model}"
    return names


def _out() -> SettingsOut:
    return SettingsOut(
        models=ModelDefaults.model_validate(app_settings.get_models()),
        initialized=app_settings.is_initialized(),
        model_names=_model_names(),
    )


@router.get("")
async def get_settings() -> SettingsOut:
    return _out()


@router.put("")
async def put_settings(body: SettingsBody) -> SettingsOut:
    app_settings.set_models(body.models.model_dump())
    return _out()


# --- Цены моделей для учёта затрат (services/prices.py) ---


@router.get("/prices")
async def get_prices() -> PricesBody:
    """Текущие цены (USD за 1M токенов). Стоимость вызова считается по ним в момент вызова."""
    return PricesBody(prices={k: ModelPrice.model_validate(v) for k, v in prices.load().items()})


@router.put("/prices")
async def put_prices(body: PricesBody) -> PricesBody:
    """Обновить цены — действует на новые вызовы (накопленные затраты не пересчитываются)."""
    saved = prices.save({k: v.model_dump() for k, v in body.prices.items()})
    return PricesBody(prices={k: ModelPrice.model_validate(v) for k, v in saved.items()})
