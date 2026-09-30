"""OpenRouter-гейт: JSON-режим + выключенный reasoning, ленивый разбор, ошибки в теле 200,
обрезка по max_tokens — без повтора того же входа."""

import asyncio

import pytest

from app.ai import openrouter as mod
from app.ai.base import AIError, AINonRetryable, loads_lenient
from app.config import settings as config


def _body(content, finish="stop", usage=None):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture()
def gate(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "test-key")
    g = mod.OpenRouterGate()
    g.posted = []

    async def fake_post(payload):
        g.posted.append(payload)
        return g.reply

    monkeypatch.setattr(g, "_post", fake_post)
    return g


def test_loads_lenient_strips_fences_and_prose():
    assert loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}
    assert loads_lenient('Вот ответ: {"a": {"b": 2}} — готово') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        loads_lenient("совсем не json")


def test_request_json_mode_and_reasoning_off(gate):
    gate.reply = _body('```json\n{"items": [{"name": "лук"}]}\n```')
    parsed, usage = asyncio.run(gate.complete_json(
        [{"role": "user", "content": "x"}], max_tokens=300, temperature=0.3, label="t",
    ))
    assert parsed == {"items": [{"name": "лук"}]} and usage["total_tokens"] == 15
    payload = gate.posted[0]
    assert payload["model"] == config.openrouter_model
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["reasoning"] == {"enabled": False}
    assert payload["max_tokens"] == 300 and payload["temperature"] == 0.3


def test_truncated_bad_json_is_non_retryable(gate):
    gate.reply = _body('{"items": [{"name": "лу', finish="length")
    with pytest.raises(AIError) as exc:
        asyncio.run(gate.complete_json([{"role": "user", "content": "x"}], retries=2, label="t"))
    assert "обрезан" in str(exc.value)
    assert len(gate.posted) == 1  # AINonRetryable — базовый ретрай не повторял вход


def test_bad_json_with_stop_is_retried(gate):
    gate.reply = _body("не json", finish="stop")
    with pytest.raises(AIError):
        asyncio.run(gate.complete_json([{"role": "user", "content": "x"}], retries=1, label="t"))
    assert len(gate.posted) == 2


def test_error_in_200_body_is_ai_error(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "test-key")
    g = mod.OpenRouterGate()

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"error": {"message": "rate-limited upstream", "code": 429}}

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", Client)
    with pytest.raises(AIError) as exc:
        asyncio.run(g._post({}))
    assert "rate-limited" in str(exc.value)


def test_not_configured(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "")
    with pytest.raises(AIError):
        asyncio.run(mod.OpenRouterGate().complete_json([{"role": "user", "content": "x"}]))


def test_isinstance_non_retryable():
    assert issubclass(AINonRetryable, AIError)
