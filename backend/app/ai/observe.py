"""Единое логирование AI-вызовов: полный промпт, ответ и usage (в т.ч. кэш токенов)."""

import json
import logging

logger = logging.getLogger("easy_week.ai")


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
