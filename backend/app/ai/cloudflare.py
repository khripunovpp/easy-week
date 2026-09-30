import json
import logging
from typing import Any

import httpx

from ..config import settings
from .base import AIError, ModelGate

_BASE = "https://api.cloudflare.com/client/v4/accounts"
logger = logging.getLogger("easy_week.cloudflare")


class CloudflareGate(ModelGate):
    """Cloudflare Workers AI: структурированный вывод по json_schema.

    Штатно используется как один из выбираемых провайдеров рецептов (пайплайн
    меню→спеки→валидатор в planner) и для вспомогательных задач (список покупок).
    """

    key = "cloudflare"
    provider = "Cloudflare"
    supports_stream = False
    supports_tools = False

    @property
    def configured(self) -> bool:
        return settings.cf_configured

    @property
    def default_model(self) -> str:
        # Быстрая модель — спеки блюд в пайплайне; выбор в настройках её не меняет.
        return settings.cf_model

    @property
    def main_model(self) -> str:
        """Главная модель (рецепты, покупки, правки, предпочтения, сводка, валидатор) —
        выбранная в настройках или CF_MODEL_JUDGE."""
        return self._model_override or settings.cf_model_judge

    @property
    def menu_model(self) -> str:
        """Модель меню плана / одного блюда — выбранная в настройках или CF_MODEL_MENU."""
        return self._model_override or settings.cf_model_menu

    def with_model(self, model_id: str | None):
        # У Cloudflare выбор меняет главную модель (default_model — спеки — остаётся).
        if not model_id or model_id == settings.cf_model_judge:
            return self
        import copy

        clone = copy.copy(self)
        clone._model_override = model_id
        return clone

    def _log_model(self, model: str) -> str:
        return model.split("/")[-1]

    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if schema is None:
            raise AIError("Cloudflare требует json_schema (schema=...)")

        url = f"{_BASE}/{settings.cf_account_id}/ai/run/{model}"
        payload = {
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": schema},
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {settings.cf_api_token}"}

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            raise AIError(f"Workers AI {resp.status_code}: {resp.text[:300]}")

        body = resp.json()
        if not body.get("success", True):
            raise AIError(f"Workers AI errors: {body.get('errors')}")

        result = body.get("result", {})
        response = result.get("response")
        if response is None:
            raise AIError(f"Пустой ответ Workers AI: {str(body)[:200]}")

        if isinstance(response, str):
            parsed = json.loads(response)  # битый JSON → ValueError → ретрай в базе
        else:
            parsed = response

        return parsed, result.get("usage", {}) or {}
