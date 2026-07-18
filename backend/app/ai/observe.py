"""Единое логирование AI-вызовов: полный промпт, ответ и usage (в т.ч. кэш токенов).

Пишем и в консоль (читаемо), и в файл-за-день JSONL (для анализа):
`<data>/ai-logs/ai-YYYY-MM-DD.jsonl` — одна строка = один вызов модели.
"""

import contextvars
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from prometheus_client import Counter

from ..config import settings

logger = logging.getLogger("easy_week.ai")

# Контекст запроса для корреляции AI-логов (conversation_id/plan_id/dish_id/endpoint/action).
# Роутер выставляет его раз на запрос — подмешивается в каждую AI-запись без протаскивания
# через сигнатуры гейтов. contextvars изолирован по задаче-запросу, между запросами не течёт.
_CTX_KEYS = ("conversation_id", "plan_id", "dish_id", "endpoint", "action")
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ai_ctx", default={})


def set_ai_context(**fields) -> None:
    """Дополнить контекст текущего запроса (None-поля игнорируются)."""
    cur = dict(_ctx.get())
    for k, v in fields.items():
        if v is not None:
            cur[k] = v
    _ctx.set(cur)


def clear_ai_context() -> None:
    _ctx.set({})

# Метрики для Prometheus. category — огрублённый label (текст до ":"), чтобы не плодить
# высокую кардинальность (в label иначе попадают названия блюд).
_calls = Counter("easyweek_ai_calls_total", "AI-вызовы", ["provider", "model", "category"])
_tokens = Counter("easyweek_ai_tokens_total", "AI-токены", ["provider", "model", "kind"])
_errors = Counter("easyweek_ai_errors_total", "Ошибки AI-вызовов", ["provider", "model", "category"])

# Бизнес-счётчики: из них в Grafana считаем токены/чат, токены/план, планы/чат.
_plans = Counter("easyweek_plans_total", "Созданные планы", ["source"])  # create | edit
_conversations = Counter("easyweek_conversations_total", "Начатые диалоги")


def record_plan(source: str = "create") -> None:
    """source=create — новый план в чате; edit — новая версия при правке."""
    _plans.labels(source).inc()


def record_conversation() -> None:
    _conversations.inc()


def _category(label: str) -> str:
    return (label or "?").split(":")[0].strip() or "?"


def _record_metrics(provider: str, model: str, label: str, usage: dict) -> None:
    _calls.labels(provider, model, _category(label)).inc()
    u = usage or {}
    for kind, key in (
        ("prompt", "prompt_tokens"),
        ("completion", "completion_tokens"),
        ("cache_hit", "prompt_cache_hit_tokens"),
        ("cache_miss", "prompt_cache_miss_tokens"),
    ):
        val = u.get(key)
        if val:
            _tokens.labels(provider, model, kind).inc(val)


def _write_file_record(record: dict) -> None:
    try:
        d = Path(settings.ai_log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fname = d / f"ai-{date.today().isoformat()}.jsonl"
        with fname.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — лог в файл не должен ронять запрос
        logger.warning("не удалось записать AI-лог в файл: %s", str(exc)[:150])


def _usage_summary(usage: dict) -> str:
    u = usage or {}
    # DeepSeek и Cloudflare отдают usage по-разному — собираем что есть.
    fields = [
        ("total", u.get("total_tokens")),
        ("prompt", u.get("prompt_tokens")),
        ("cache_hit", u.get("prompt_cache_hit_tokens")),
        ("cache_miss", u.get("prompt_cache_miss_tokens")),
        ("completion", u.get("completion_tokens")),
    ]
    return " ".join(f"{name}={val}" for name, val in fields if val is not None) or "—"


def _base_record() -> dict:
    """Каркас записи AI-лога: ts + корреляционный контекст запроса."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ctx = _ctx.get()
    for k in _CTX_KEYS:
        if ctx.get(k) is not None:
            rec[k] = ctx[k]
    return rec


def log_ai_call(
    provider: str,
    model: str,
    label: str,
    messages: list[dict],
    response: object,
    usage: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    """Полный лог одного успешного вызова модели: промпт, ответ, usage, длительность."""
    resp = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
    logger.info("AI ← %s · %s · %s", provider, model, label or "?")
    logger.info("  usage: %s", _usage_summary(usage or {}))
    logger.info("  prompt: %s", json.dumps(messages, ensure_ascii=False))
    logger.info("  response: %s", resp)

    _record_metrics(provider, model, label, usage or {})

    rec = _base_record()
    rec.update(
        {
            "provider": provider,
            "model": model,
            "label": label,
            "ok": True,
            "duration_ms": duration_ms,
            "usage": usage or {},
            "messages": messages,
            "response": response,
        }
    )
    _write_file_record(rec)


def log_ai_error(
    provider: str,
    model: str,
    label: str,
    messages: list[dict],
    error: str,
    attempt: int,
    duration_ms: int | None = None,
) -> None:
    """Лог неудачной попытки вызова (в JSONL, для анализа флаки-паттернов).

    Консольный WARNING пишет вызывающий (base.complete_json) — тут только файл + метрика."""
    _errors.labels(provider, model, _category(label)).inc()
    rec = _base_record()
    rec.update(
        {
            "provider": provider,
            "model": model,
            "label": label,
            "ok": False,
            "attempt": attempt,
            "error": (error or "")[:500],
            "duration_ms": duration_ms,
            "messages": messages,
        }
    )
    _write_file_record(rec)
