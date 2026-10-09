"""Дневные лимиты генерации — защита от расхода на дорогих моделях и чужих квот.

Claude (anthropic): ANTHROPIC_DAILY_PLANS / ANTHROPIC_DAILY_RECIPES, по умолчанию 0 — без лимита
(расход видно в статистике запросов: Профиль → Модели, services/usage). Озвучка шагов (TTS): свой лимит
TTS_DAILY_LIMIT (по умолчанию 20 генераций в сутки) — бережёт общий дневной лимит бесплатных
моделей OpenRouter. Остальные провайдеры (DeepSeek/Gemini/Cloudflare) — без лимитов.

Счётчик персистентный: JSON-файл рядом с БД (переживает рестарты/деплой),
сбрасывается по смене даты (локальной).
"""

import json
import logging
from datetime import date
from pathlib import Path

from ..config import settings
from .base import LimitError  # noqa: F401 — живёт в base (её пробрасывает complete_json)

logger = logging.getLogger("easy_week.limits")


_KIND_RU = {"plan": ("план", "плана", "планов"), "recipe": ("рецепт", "рецепта", "рецептов")}


def _count_word(n: int, kind: str) -> str:
    """«1 план» / «2 плана» / «5 планов» (было «2 планов в день»)."""
    one, few, many = _KIND_RU.get(kind, (kind, kind, kind))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


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


# --- Озвучка шагов ---
# Бронь до вызова модели + возврат при сбое: считаем только УДАВШИЕСЯ генерации, а два
# параллельных синтеза (прогрев) не проскочат лимит (между чтением и записью файла нет await).


def tts_status() -> dict:
    """Расход дневного лимита озвучки: used/limit/remaining (limit 0 — без лимита)."""
    limit = settings.tts_daily_limit
    used = int(_read_today().get("tts", 0))
    return {"used": used, "limit": limit, "remaining": max(0, limit - used) if limit > 0 else -1}


def tts_reserve() -> bool:
    """Забронировать одну генерацию озвучки. False — дневной лимит исчерпан."""
    limit = settings.tts_daily_limit
    if limit <= 0:
        return True
    data = _read_today()
    used = int(data.get("tts", 0))
    if used >= limit:
        return False
    data["tts"] = used + 1
    _write(data)
    return True


def tts_refund() -> None:
    """Вернуть бронь (синтез не удался — он не должен съедать лимит)."""
    if settings.tts_daily_limit <= 0:
        return
    data = _read_today()
    data["tts"] = max(0, int(data.get("tts", 0)) - 1)
    _write(data)


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
            f"Дневной лимит Claude исчерпан: {_count_word(limit, kind)} в день. "
            f"Переключитесь на DeepSeek или Gemini, либо попробуйте завтра."
        )
    data[key] = used + 1
    _write(data)
    logger.info("limit anthropic %s: %d/%d", kind, used + 1, limit)
