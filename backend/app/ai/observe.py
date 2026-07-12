"""Единое логирование AI-вызовов: полный промпт, ответ и usage (в т.ч. кэш токенов).

Пишем и в консоль (читаемо), и в файл-за-день JSONL (для анализа):
`<data>/ai-logs/ai-YYYY-MM-DD.jsonl` — одна строка = один вызов модели.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import settings

logger = logging.getLogger("easy_week.ai")


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


def log_ai_call(
    provider: str,
    model: str,
    label: str,
    messages: list[dict],
    response: object,
    usage: dict | None = None,
) -> None:
    """Полный лог одного вызова модели: промпт (все сообщения), ответ, usage."""
    resp = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
    logger.info("AI ← %s · %s · %s", provider, model, label or "?")
    logger.info("  usage: %s", _usage_summary(usage or {}))
    logger.info("  prompt: %s", json.dumps(messages, ensure_ascii=False))
    logger.info("  response: %s", resp)

    _write_file_record(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "label": label,
            "usage": usage or {},
            "messages": messages,
            "response": response,
        }
    )
