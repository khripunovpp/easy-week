"""Единое логирование AI-вызовов: полный промпт, ответ и usage (в т.ч. кэш токенов).

Пишем и в консоль (читаемо), и в файл-за-день JSONL (для анализа):
`<data>/ai-logs/ai-YYYY-MM-DD.jsonl` — одна строка = один вызов модели.
"""

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from prometheus_client import Counter

from ..config import settings
from ..services import prices

logger = logging.getLogger("easy_week.ai")

# Контекст запроса для корреляции AI-логов (conversation_id/plan_id/dish_id/endpoint/action).
# Роутер выставляет его раз на запрос — подмешивается в каждую AI-запись без протаскивания
# через сигнатуры гейтов. contextvars изолирован по задаче-запросу, между запросами не течёт.
# variety — серверное «зерно разнообразия» плана (кухня/способы/белки), для анализа повторов.
# gen_id — id одной генерации рецепта (он же пишется в вариант рецепта): строка AI-лога ↔
# версия рецепта; recipe_id — рецепт (таблицы рецептов, следующие фазы). Только корреляция.
_CTX_KEYS = (
    "conversation_id", "plan_id", "dish_id", "endpoint", "action", "variety", "recipe_id",
    "gen_id",
)
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ai_ctx", default={})


def set_ai_context(**fields) -> None:
    """Дополнить контекст текущего запроса (None-поля игнорируются)."""
    cur = dict(_ctx.get())
    for k, v in fields.items():
        if v is not None:
            cur[k] = v
    _ctx.set(cur)


@contextmanager
def ai_scope(**fields) -> Iterator[None]:
    """Дополнить контекст только на время одного вызова (напр. gen_id генерации рецепта):
    после выхода — прежний контекст, чтобы следующий AI-вызов того же запроса (план готовки
    после догенерации рецептов) не унёс чужой gen_id в свою строку лога."""
    cur = dict(_ctx.get())
    cur.update({k: v for k, v in fields.items() if v is not None})
    token = _ctx.set(cur)
    try:
        yield
    finally:
        _ctx.reset(token)


def clear_ai_context() -> None:
    _ctx.set({})

# Метрики для Prometheus. category — огрублённый label (текст до ":"), чтобы не плодить
# высокую кардинальность (в label иначе попадают названия блюд).
_calls = Counter("easyweek_ai_calls_total", "AI-вызовы", ["provider", "model", "category"])
_tokens = Counter("easyweek_ai_tokens_total", "AI-токены", ["provider", "model", "kind"])
_errors = Counter("easyweek_ai_errors_total", "Ошибки AI-вызовов", ["provider", "model", "category"])
# Затраты в USD по текущей цене на момент вызова (таблица цен — services/prices.py).
_cost = Counter("easyweek_ai_cost_usd_total", "Затраты на AI-вызовы, USD", ["provider", "model", "category"])

# Метка провайдера в логах → ключ модели в таблице цен (без импорта gates — цикл импортов).
_PROVIDER_KEY = {
    "deepseek": "deepseek", "claude": "anthropic", "gemini": "gemini", "cloudflare": "cloudflare",
    "openrouter": "openrouter",
}


def _norm_cache(usage: dict) -> dict:
    """Кэш у провайдеров называется по-разному — приводим к prompt_cache_hit_tokens.
    DeepSeek/Claude/Gemini уже нормализованы в своих гейтах; Cloudflare и OpenRouter
    (OpenAI-формат) отдают prompt_tokens_details.cached_tokens."""
    u = dict(usage or {})
    if u.get("prompt_cache_hit_tokens") is None:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached:
            u["prompt_cache_hit_tokens"] = cached
    return u

# Бизнес-счётчики: из них в Grafana считаем токены/чат, токены/план, планы/чат.
_plans = Counter("easyweek_plans_total", "Созданные планы", ["source"])  # create | edit
_conversations = Counter("easyweek_conversations_total", "Начатые диалоги")
# Оценки ответов моделей — из них в Grafana: 👍/👎 по моделям и типам ответов.
_ratings = Counter("easyweek_ratings_total", "Оценки ответов", ["target_type", "model", "vote"])
# Причины 👎 (ключи из services/rating_reasons) — топ жалоб по моделям.
_rating_reasons = Counter(
    "easyweek_rating_reasons_total", "Причины 👎", ["target_type", "model", "reason"]
)


def record_plan(source: str = "create") -> None:
    """source=create — новый план в чате; edit — новая версия при правке."""
    _plans.labels(source).inc()


def record_rating(target_type: str, model: str, vote: int) -> None:
    """vote: 1 (👍) | -1 (👎). model — ключ модели (пусто → 'unknown')."""
    label = "up" if vote > 0 else "down"
    _ratings.labels(target_type or "?", model or "unknown", label).inc()


def record_rating_reasons(target_type: str, model: str, reasons: list[str]) -> None:
    for r in reasons:
        _rating_reasons.labels(target_type or "?", model or "unknown", r).inc()


def record_conversation() -> None:
    _conversations.inc()


def _category(label: str) -> str:
    return (label or "?").split(":")[0].strip() or "?"


def call_cost(provider: str, model: str, usage: dict) -> float:
    """Стоимость вызова (USD) по текущей таблице цен: метки провайдера из лога → ключ цены.
    Нужна метрикам и статистике по дням (у старых записей лога cost_usd нет)."""
    key = _PROVIDER_KEY.get((provider or "").lower(), "")
    return prices.cost_usd(key, _norm_cache(usage), model)


def provider_key(provider: str) -> str:
    """Метка провайдера в логе («Claude») → ключ модели («anthropic»); неизвестная — как есть."""
    return _PROVIDER_KEY.get((provider or "").lower(), provider or "")


def _record_metrics(provider: str, model: str, label: str, usage: dict) -> float:
    """Счётчики вызова/токенов/затрат. Возвращает стоимость вызова (USD) — для JSONL."""
    _calls.labels(provider, model, _category(label)).inc()
    u = _norm_cache(usage)
    for kind, key in (
        ("prompt", "prompt_tokens"),
        ("completion", "completion_tokens"),
        ("cache_hit", "prompt_cache_hit_tokens"),
        ("cache_miss", "prompt_cache_miss_tokens"),
        ("cache_write", "prompt_cache_write_tokens"),
        ("neurons", "neurons"),
    ):
        val = u.get(key)
        if val:
            _tokens.labels(provider, model, kind).inc(val)
    try:
        cost = call_cost(provider, model, u)
    except Exception as exc:  # noqa: BLE001 — учёт затрат не должен ронять запрос
        logger.warning("не посчитали стоимость вызова: %s", str(exc)[:150])
        cost = 0.0
    if cost:
        _cost.labels(provider, model, _category(label)).inc(cost)
    return cost


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

    cost = _record_metrics(provider, model, label, usage or {})

    rec = _base_record()
    rec.update(
        {
            "provider": provider,
            "model": model,
            "label": label,
            "ok": True,
            "duration_ms": duration_ms,
            "usage": usage or {},
            "cost_usd": round(cost, 6),
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
    extra: dict | None = None,
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
    if extra:  # напр. stop_reason + сырой сниппет ответа при битом JSON
        rec.update(extra)
    _write_file_record(rec)
