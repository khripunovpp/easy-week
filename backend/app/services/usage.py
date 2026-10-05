"""Статистика запросов к моделям по дням (Профиль → Модели, GET /api/settings/usage).

Источник — AI-логи (`data/ai-logs/ai-ГГГГ-ММ-ДД.jsonl`, строка на попытку вызова — пишут
observe.log_ai_call / log_ai_error): удачный вызов — запись с ok≠false (у старых записей поля ok
нет), сбой — ok=false (каждая неудачная попытка, повторы тоже). Стоимость — cost_usd записи (по цене
на момент вызова), у старых записей без неё — по текущей таблице цен (observe.call_cost).
День — по файлу лога (локальная дата сервера). Разбор файла кэшируется по (размер, mtime):
прошлые дни не меняются, сегодняшний перечитывается, только когда дописан.
"""

import json
import logging
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from ..ai.observe import call_cost, provider_key
from ..config import settings

logger = logging.getLogger("easy_week.usage")

MAX_DAYS = 31


@lru_cache(maxsize=64)
def _file_stats(path: str, size: int, mtime_ns: int) -> tuple[tuple[str, int, int, float], ...]:
    """(провайдер, вызовы, сбои, USD) по одному файлу лога. size/mtime — ключ кэша."""
    acc: dict[str, list] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # недописанная/битая строка — пропускаем
            provider = str(rec.get("provider") or "?")
            row = acc.setdefault(provider, [0, 0, 0.0])
            if rec.get("ok") is False:
                row[1] += 1
                continue
            row[0] += 1
            cost = rec.get("cost_usd")
            if cost is None:
                try:
                    cost = call_cost(provider, str(rec.get("model") or ""), rec.get("usage") or {})
                except Exception:  # noqa: BLE001 — старая запись без цены не ломает статистику
                    cost = 0.0
            row[2] += float(cost or 0)
    return tuple((p, c, e, usd) for p, (c, e, usd) in acc.items())


def _day(d: date) -> dict:
    path = Path(settings.ai_log_dir) / f"ai-{d.isoformat()}.jsonl"
    try:
        st = path.stat()
        rows = _file_stats(str(path), st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        rows = ()
    except OSError as exc:
        logger.warning("статистика: не прочитали %s: %s", path.name, exc)
        rows = ()
    providers = sorted(
        (
            {"provider": p, "key": provider_key(p), "calls": c, "errors": e,
             "cost_usd": round(usd, 4)}
            for p, c, e, usd in rows
        ),
        key=lambda x: (-x["calls"], -x["errors"], x["provider"]),
    )
    return {
        "date": d.isoformat(),
        "calls": sum(p["calls"] for p in providers),
        "errors": sum(p["errors"] for p in providers),
        "cost_usd": round(sum(usd for *_, usd in rows), 4),
        "providers": providers,
    }


def daily(days: int = 7, today: date | None = None) -> dict:
    """Последние days дней (сегодня первым, пустые дни — с нулями) + итог за период."""
    today = today or date.today()
    out = [_day(today - timedelta(days=i)) for i in range(max(1, min(days, MAX_DAYS)))]
    total = {
        "calls": sum(d["calls"] for d in out),
        "errors": sum(d["errors"] for d in out),
        "cost_usd": round(sum(d["cost_usd"] for d in out), 4),
    }
    return {"days": out, "total": total}
