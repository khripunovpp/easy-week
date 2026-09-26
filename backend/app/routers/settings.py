"""Общие настройки приложения (одни на все устройства семьи): модели по умолчанию по задачам.

GET /api/settings  → {models: {chat, recipe, shopping, cooking}, initialized}
PUT /api/settings  ← {models: {...}} — ключи моделей валидирует схема (Literal ключей GATES).
Хранение — `services/settings.py` (settings.json рядом с БД, атомарная запись).
"""

from fastapi import APIRouter

from ..schemas import ModelDefaults, SettingsBody, SettingsOut
from ..services import settings as app_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _out() -> SettingsOut:
    return SettingsOut(
        models=ModelDefaults.model_validate(app_settings.get_models()),
        initialized=app_settings.is_initialized(),
    )


@router.get("")
async def get_settings() -> SettingsOut:
    return _out()


@router.put("")
async def put_settings(body: SettingsBody) -> SettingsOut:
    app_settings.set_models(body.models.model_dump())
    return _out()
