"""Дневные лимиты генерации — защита от расхода на дорогих моделях.

Сейчас лимитируется только Claude (anthropic): план дорог, поэтому режем частоту.
Остальные провайдеры (DeepSeek/Gemini/Cloudflare) — без лимитов.

Счётчик персистентный: JSON-файл рядом с БД (переживает рестарты/деплой),
сбрасывается по смене даты (локальной).
"""

import json
import logging
from datetime import date
from pathlib import Path

from ..config import settings
from .base import AIError

logger = logging.getLogger("easy_week.limits")


class LimitError(AIError):
    """Дневной лимит генерации на модель исчерпан."""


_KIND_RU = {"plan": "планов", "recipe": "рецептов"}


def _limit_for(kind: str) -> int:
    if kind == "plan":
        return settings.anthropic_daily_plans
    if kind == "recipe":
        return settings.anthropic_daily_recipes
    return 0


def _file() -> Path:
    return Path(settings.db_path).parent / "usage-limits.json"


def _read_today() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый → начинаем день заново
        data = {}
    if data.get("date") != date.today().isoformat():
        return {"date": date.today().isoformat()}
    return data


def _write(data: dict) -> None:
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — счётчик не должен ронять запрос
        logger.warning("limits write failed: %s", str(exc)[:120])


def status() -> dict:
    """Текущий расход дневных лимитов Claude за сегодня: used/limit/remaining."""
    data = _read_today()

    def one(kind: str) -> dict:
        limit = _limit_for(kind)
        used = int(data.get(f"anthropic_{kind}", 0))
        return {"used": used, "limit": limit, "remaining": max(0, limit - used)}

    return {"plans": one("plan"), "recipes": one("recipe")}


def enforce_daily(gate, kind: str) -> None:
    """Проверить и увеличить дневной счётчик. Только для Claude, иначе no-op.

    kind: 'plan' | 'recipe'. Превышение лимита → LimitError (до вызова модели,
    так что заблокированный запрос токенов не тратит).
    """
    if getattr(gate, "key", "") != "anthropic":
        return
    limit = _limit_for(kind)
    if limit <= 0:
        return
    data = _read_today()
    key = f"anthropic_{kind}"
    used = int(data.get(key, 0))
    if used >= limit:
        raise LimitError(
            f"Дневной лимит Claude исчерпан: {limit} {_KIND_RU.get(kind, kind)} в день. "
            f"Переключитесь на DeepSeek или Gemini, либо попробуйте завтра."
        )
    data[key] = used + 1
    _write(data)
    logger.info("limit anthropic %s: %d/%d", kind, used + 1, limit)
