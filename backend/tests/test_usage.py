"""Статистика запросов к моделям по дням (Профиль → Модели) — из AI-логов."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

from app.ai import limits
from app.config import settings
from app.routers import settings as settings_router
from app.services import usage


def _log(dir_, day: str, records: list[dict]) -> None:
    (dir_ / f"ai-{day}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records) + '{"обрыв строки',
        encoding="utf-8",
    )


def test_daily_counts_calls_errors_and_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "settings", SimpleNamespace(ai_log_dir=str(tmp_path)))
    _log(tmp_path, "2026-10-05", [
        {"provider": "Claude", "model": "claude-haiku-4-5", "ok": True, "cost_usd": 0.03},
        {"provider": "Claude", "model": "claude-haiku-4-5", "ok": True, "cost_usd": 0.02},
        {"provider": "DeepSeek", "model": "deepseek-chat", "ok": True, "cost_usd": 0.001},
        {"provider": "Gemini", "model": "gemini-flash-latest", "ok": False, "attempt": 1},
        {"provider": "Gemini", "model": "gemini-flash-latest", "ok": False, "attempt": 2},
    ])
    # старый формат: без ok и cost_usd — вызов, стоимость по текущим ценам
    _log(tmp_path, "2026-10-03", [
        {"provider": "DeepSeek", "model": "deepseek-chat",
         "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
    ])
    out = usage.daily(3, today=date(2026, 10, 5))
    today, empty, old = out["days"]
    assert [d["date"] for d in out["days"]] == ["2026-10-05", "2026-10-04", "2026-10-03"]
    assert (today["calls"], today["errors"], today["cost_usd"]) == (3, 2, 0.051)
    assert [(p["provider"], p["key"], p["calls"], p["errors"]) for p in today["providers"]] == [
        ("Claude", "anthropic", 2, 0), ("DeepSeek", "deepseek", 1, 0), ("Gemini", "gemini", 0, 2),
    ]
    assert empty == {"date": "2026-10-04", "calls": 0, "errors": 0, "cost_usd": 0, "providers": []}
    assert old["calls"] == 1 and old["cost_usd"] > 0  # цена DeepSeek за 1M входных токенов
    assert out["total"]["calls"] == 4 and out["total"]["errors"] == 2


def test_usage_endpoint_camel_case(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "settings", SimpleNamespace(ai_log_dir=str(tmp_path)))
    _log(tmp_path, date.today().isoformat(), [{"provider": "Cloudflare", "ok": True, "cost_usd": 0}])
    body = asyncio.run(settings_router.get_usage(days=2)).model_dump(by_alias=True)
    assert body["days"][0]["providers"][0] == {
        "calls": 1, "errors": 0, "costUsd": 0.0, "provider": "Cloudflare", "key": "cloudflare",
    }
    assert len(body["days"]) == 2 and body["total"]["calls"] == 1


def test_claude_limits_off_by_default():
    # лимиты Claude сняты: расход видно в статистике; включаются через .env
    assert settings.anthropic_daily_plans == 0 and settings.anthropic_daily_recipes == 0
    gate = SimpleNamespace(key="anthropic")
    for _ in range(50):
        limits.enforce_daily(gate, "plan")  # не кидает LimitError
