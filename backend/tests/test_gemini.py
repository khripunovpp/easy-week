"""Gemini-гейт: перегрузка (503) — повтор с паузой 2–6 с, квотный 429 — без повтора и с
«заблокировано до», MAX_TOKENS/фильтр — без повтора, битый JSON — в JSONL с сырым текстом."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.ai import base, observe, planner
from app.ai import gemini as gm
from app.ai.base import AIError, AINonRetryable, AIOverloaded, LimitError, ModelGate
from app.ai.limits import LimitError as LimitsLimitError
from app.config import settings as config
from app.routers import recipes as recipes_router
from app.schemas import RecipeTextBody

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

OVERLOAD = {"error": {
    "code": 503, "status": "UNAVAILABLE",
    "message": "This model is currently experiencing high demand. Spikes in demand are usually "
               "temporary. Please try again later.",
}}


def _quota(quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", delay="33s"):
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": quota_id, "quotaValue": "10",
        }]},
        {"@type": "type.googleapis.com/google.rpc.Help", "links": [{"url": "https://ai.dev"}]},
    ]
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        # Длинное сообщение: quotaId — далеко за 300 символами (раньше обрезался).
        "message": "You exceeded your current quota, please check your plan and billing details. "
                   "For more information on this error, head to: https://ai.google.dev/gemini-api/"
                   "docs/rate-limits. To monitor your current usage, head to: https://ai.dev/usage"
                   "?tab=rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis."
                   "com/generate_content_free_tier_requests, limit: 10, model: gemini-3-flash",
        "details": details,
    }}


def _ok(text, finish="STOP"):
    return {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"},
                        "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50,
                          "totalTokenCount": 150},
        "modelVersion": "gemini-3-flash",
    }


@pytest.fixture()
def api(monkeypatch):
    """Подменённый Gemini API: очередь ответов (status, json, headers), счётчик запросов и
    паузы ретраев (asyncio.sleep в base — без реального ожидания)."""
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(gm, "_blocked", {})
    state = SimpleNamespace(queue=[], posted=[], sleeps=[])

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            state.posted.append(url)
            status, body, hdrs = state.queue.pop(0)
            return httpx.Response(status, json=body, headers=hdrs or {})

    async def fake_sleep(sec):
        state.sleeps.append(sec)

    monkeypatch.setattr(gm.httpx, "AsyncClient", Client)
    monkeypatch.setattr(base, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return state


@pytest.fixture()
def jsonl(monkeypatch):
    """Записи AI-лога (JSONL) этого теста — вместо файла."""
    recs: list[dict] = []
    monkeypatch.setattr(observe, "_write_file_record", recs.append)
    return recs


def _run(gate=None, **kw):
    gate = gate or gm.GeminiGate()
    return asyncio.run(gate.complete_json(MSGS, label="тест", **kw))


def test_overload_then_ok_waits_longer_with_jitter(api, jsonl):
    api.queue = [(503, OVERLOAD, None), (200, _ok('{"a": 1}'), None)]
    parsed, usage = _run()
    assert parsed == {"a": 1} and usage["total_tokens"] == 150
    assert len(api.posted) == 2
    assert len(api.sleeps) == 1 and 2.0 <= api.sleeps[0] <= 4.0  # не 0.4 с
    fail = [r for r in jsonl if r["ok"] is False][0]
    assert fail["http_status"] == 503 and fail["error_status"] == "UNAVAILABLE"
    assert "high demand" in fail["error_message"]


def test_overload_all_attempts_backoff_grows(api, jsonl):
    api.queue = [(503, OVERLOAD, None)] * 3
    with pytest.raises(AIError) as err:
        _run(retries=2)
    assert not isinstance(err.value, LimitError)
    assert "503" in str(err.value) and "high demand" in str(err.value)  # фронт: «перегружена»
    assert len(api.posted) == 3
    assert 2.0 <= api.sleeps[0] <= 4.0 and 4.0 <= api.sleeps[1] <= 6.0
    assert sum(api.sleeps) <= gm.GeminiGate.retry_sleep_budget


def test_retry_delay_from_body_respected_and_long_one_not_waited(api, jsonl):
    short = {"error": {**OVERLOAD["error"], "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7.5s"}]}}
    api.queue = [(503, short, None), (200, _ok('{"a": 1}'), None)]
    assert _run()[0] == {"a": 1}
    assert api.sleeps == [7.5]

    api.posted.clear()
    api.sleeps.clear()
    api.queue = [(503, OVERLOAD, {"Retry-After": "120"})]
    with pytest.raises(AIError):
        _run(retries=2)
    assert len(api.posted) == 1 and api.sleeps == []  # 120 с — сверх бюджета, не ждём


def test_quota_429_not_retried_and_next_call_blocked(api, jsonl):
    api.queue = [(429, _quota(), None)]
    with pytest.raises(LimitError) as err:
        _run(retries=2)
    assert len(api.posted) == 1 and api.sleeps == []  # повтора нет
    assert "Квота запросов Gemini исчерпана" in str(err.value)
    assert 25 < err.value.until - time.time() <= 33.5  # retryDelay из тела
    rec = [r for r in jsonl if r["ok"] is False][0]
    # quotaId и полное тело — в JSONL (раньше тело резалось на 300 символах)
    assert rec["quota_ids"] == ["GenerateRequestsPerMinutePerProjectPerModel-FreeTier"]
    assert rec["retry_delay_s"] == 33.0 and rec["error_status"] == "RESOURCE_EXHAUSTED"
    assert "QuotaFailure" in rec["error_body"]

    # Следующий вызов — сразу LimitError, в Gemini не ходим и попыткой в лог не пишем.
    n = len(jsonl)
    with pytest.raises(LimitError) as err2:
        _run()
    assert len(api.posted) == 1 and len(jsonl) == n
    assert "Gemini" in str(err2.value)
    # Другая модель Gemini — своя квота, не заблокирована.
    api.queue = [(200, _ok('{"b": 2}'), None)]
    assert _run(gm.GeminiGate().with_model("gemini-flash-lite-latest"))[0] == {"b": 2}


def test_daily_quota_blocks_until_pacific_midnight(api, jsonl):
    api.queue = [(429, _quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "41s"), None)]
    with pytest.raises(LimitError) as err:
        _run()
    assert "Дневная квота Gemini исчерпана" in str(err.value)
    assert err.value.until == pytest.approx(gm._quota_reset(), abs=5)  # не через 41 с
    assert 0 < err.value.until - time.time() <= 25 * 3600


def test_quota_maps_to_429_in_router(api, jsonl, monkeypatch):
    """Свой рецепт на исчерпанной квоте: роутер — 429 с понятным текстом (как лимит Claude)."""
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gm.GeminiGate())
    api.queue = [(429, _quota(), None)]
    with pytest.raises(HTTPException) as err:
        asyncio.run(recipes_router.improve_recipe(RecipeTextBody(text="сырники")))
    assert err.value.status_code == 429 and "Квота запросов Gemini" in err.value.detail
    with pytest.raises(HTTPException) as err:  # повтор — сразу 429, без запроса в Gemini
        asyncio.run(recipes_router.improve_recipe(RecipeTextBody(text="сырники")))
    assert err.value.status_code == 429 and len(api.posted) == 1


def test_rate_429_without_quota_is_overload(api, jsonl):
    busy = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "Resource has been exhausted (e.g. check quota)."}}
    api.queue = [(429, busy, None), (200, _ok('{"a": 1}'), None)]
    assert _run()[0] == {"a": 1}
    assert len(api.sleeps) == 1 and 2.0 <= api.sleeps[0] <= 4.0
    assert gm._blocked == {}


def test_max_tokens_not_retried(api, jsonl):
    api.queue = [(200, _ok('{"dishes": [{"name": "бор', finish="MAX_TOKENS"), None)]
    with pytest.raises(AIError) as err:
        _run(retries=2)
    assert "MAX_TOKENS" in str(err.value) and len(api.posted) == 1
    rec = [r for r in jsonl if r["ok"] is False][0]
    assert rec["stop_reason"] == "MAX_TOKENS" and rec["usage"]["completion_tokens"] == 50


def test_safety_not_retried(api, jsonl):
    api.queue = [(200, _ok("", finish="SAFETY"), None)]
    with pytest.raises(AIError) as err:
        _run(retries=2)
    assert "SAFETY" in str(err.value) and len(api.posted) == 1


def test_bad_request_not_retried(api, jsonl):
    bad = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad thinkingConfig"}}
    api.queue = [(400, bad, None)]
    with pytest.raises(AIError) as err:
        _run(retries=2)
    assert "INVALID_ARGUMENT" in str(err.value) and len(api.posted) == 1


def test_broken_json_logs_details_and_is_retried(api, jsonl):
    steps = ", ".join(f'"шаг {i}"' for i in range(60))
    broken = '{"name": "Борщ", "ingredients" [{"name": "свёкла"}], "steps": [' + steps + "]}"
    api.queue = [(200, _ok(broken), None), (200, _ok('```json\n{"a": 1}\n```'), None)]
    parsed, _ = _run()
    assert parsed == {"a": 1}  # ленивый разбор снимает ```-ограждение
    assert len(api.posted) == 2 and api.sleeps == [0.4]  # не перегрузка — прежняя пауза
    rec = [r for r in jsonl if r["ok"] is False][0]
    assert "Expecting ':' delimiter" in rec["error"]
    assert rec["stop_reason"] == "STOP" and rec["raw_len"] == len(broken)
    assert rec["raw_head"].startswith('{"name": "Борщ"') and "raw_tail" in rec
    assert '"ingredients" [' in rec["raw_near_error"]
    assert rec["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                            "prompt_cache_hit_tokens": None}
    json.dumps(rec, ensure_ascii=False)  # запись сериализуется в JSONL


def test_other_gates_keep_short_backoff():
    """Без overload_backoff (DeepSeek, Claude, …) перегрузка ждёт как раньше: 0.4·n с."""

    class Plain(ModelGate):
        configured = True
        default_model = "m"

        async def _request_json(self, *a):
            raise AssertionError

    g = Plain()
    assert g._retry_delay(AIOverloaded("x"), 0) == 0.4 and g._retry_delay(AIError("x"), 1) == 0.8
    assert g._retry_delay(AIError("x", retry_after=3), 0) == 3


def test_limit_error_is_shared():
    assert LimitsLimitError is LimitError and issubclass(LimitError, AINonRetryable)
