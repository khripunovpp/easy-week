"""Цены AI-моделей для учёта затрат: `backend/data/prices.json`, правятся в приложении.

Цены у провайдеров меняются часто, поэтому они НЕ зашиты в код/дашборд: таблицу правит
пользователь (Профиль → «Цены моделей»), а стоимость считается В МОМЕНТ вызова по текущей
цене и копится в Prometheus-счётчике `easyweek_ai_cost_usd_total`. Смена цены влияет только
на новые вызовы — история не пересчитывается задним числом.

Все цены — USD за 1M токенов. Cloudflare Workers AI биллит «нейроны» и сам возвращает их
число в usage — для него основная цена `per_1k_neurons` (токенные — запасной вариант).
Бесплатные 10 000 нейронов/день Cloudflare не вычитаем (считаем «сколько стоило бы»).
"""

import json
import logging
import os
import threading
from pathlib import Path

from ..config import settings

logger = logging.getLogger("easy_week.prices")

# Стартовые значения (сверены 2026-09-26 со страницами провайдеров; дальше правит пользователь).
# DeepSeek: у deepseek-chat есть пиковые часы (x2) — берём ПИКОВЫЕ ставки (оценка сверху).
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "deepseek": {"input": 0.30, "cached_input": 0.006, "cache_write": 0.30, "output": 1.20},
    "gemini": {"input": 0.75, "cached_input": 0.075, "cache_write": 0.75, "output": 3.75},
    # Claude Haiku 4.5: чтение кэша 0.1x, запись 5-мин кэша 1.25x от input.
    "anthropic": {"input": 1.00, "cached_input": 0.10, "cache_write": 1.25, "output": 5.00},
    "cloudflare": {
        "input": 0.351, "cached_input": 0.351, "cache_write": 0.351, "output": 0.555,
        "per_1k_neurons": 0.011,
    },
}
PRICE_FIELDS = ("input", "cached_input", "cache_write", "output", "per_1k_neurons")

_lock = threading.Lock()


def _path() -> Path:
    return Path(settings.db_path).parent / "prices.json"


def load() -> dict[str, dict[str, float]]:
    """Текущие цены: дефолты, поверх — сохранённые пользователем (битый файл → дефолты)."""
    out = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as exc:
        logger.warning("prices.json не прочитан, берём дефолты: %s", exc)
        return out
    for key, row in (saved or {}).items():
        if key in out and isinstance(row, dict):
            for f in PRICE_FIELDS:
                if isinstance(row.get(f), (int, float)) and row[f] >= 0:
                    out[key][f] = float(row[f])
    return out


def save(prices: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Атомарная запись (tmp + os.replace): сбой посреди записи не оставит битый файл."""
    with _lock:
        merged = load()
        for key, row in prices.items():
            if key in merged:
                for f in PRICE_FIELDS:
                    if f in row and row[f] is not None:
                        merged[key][f] = float(row[f])
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return merged


def cost_usd(model_key: str, usage: dict) -> float:
    """Стоимость одного вызова по нормализованному usage (см. observe._norm_cache)."""
    price = load().get(model_key)
    if not price or not usage:
        return 0.0
    if model_key == "cloudflare" and usage.get("neurons"):
        return float(usage["neurons"]) / 1000 * price.get("per_1k_neurons", 0)
    prompt = usage.get("prompt_tokens") or 0
    hit = usage.get("prompt_cache_hit_tokens") or 0
    write = usage.get("prompt_cache_write_tokens") or 0
    fresh = max(prompt - hit - write, 0)  # prompt_tokens включает кэш-чтение и кэш-запись
    out = usage.get("completion_tokens") or 0
    return (
        fresh * price["input"]
        + hit * price["cached_input"]
        + write * price["cache_write"]
        + out * price["output"]
    ) / 1_000_000
