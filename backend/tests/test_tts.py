"""Озвучка шагов: кэш по тексту, склейка параллельных запросов, 422/502, лог и ошибки синтеза."""

import asyncio
import shutil

import pytest
from fastapi.testclient import TestClient

from app.ai import tts as tts_ai
from app.ai.base import AIError
from app.config import settings as config
from app.main import app
from app.routers import tts as tts_router


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "app_password", "")
    monkeypatch.setattr(tts_ai, "blocked_until", 0.0)
    monkeypatch.setattr(tts_ai, "blocked_reason", "")
    from app.ai import limits

    limits._file().unlink(missing_ok=True)  # счётчики дневных лимитов — с нуля
    shutil.rmtree(tts_router.cache_dir(), ignore_errors=True)
    yield
    shutil.rmtree(tts_router.cache_dir(), ignore_errors=True)


def _fake_synth(calls):
    async def synth(text):
        calls.append(text)
        return b"ID3fake"

    return synth


def test_speak_caches_and_normalizes_text(monkeypatch):
    calls = []
    monkeypatch.setattr(tts_router.tts_ai, "synthesize", _fake_synth(calls))
    with TestClient(app) as c:
        r = c.get("/api/tts", params={"text": "Нарежьте  лук\n полукольцами"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("audio/mpeg")
        assert r.content == b"ID3fake" and "no-cache" in r.headers["cache-control"]
        assert r.headers.get("etag")  # ревалидация: смена голоса → новый файл → новый ETag
        # Тот же текст с другими пробелами — из кэша, без второго вызова модели.
        assert c.get("/api/tts", params={"text": "Нарежьте лук полукольцами"}).status_code == 200
    assert calls == ["Нарежьте лук полукольцами"]
    assert len(list(tts_router.cache_dir().glob("*.mp3"))) == 1


def test_cache_key_depends_on_model_and_voice(monkeypatch):
    a = tts_router.cache_key("шаг")
    monkeypatch.setattr(config, "openrouter_tts_voice", "flux-alexis-en")
    assert tts_router.cache_key("шаг") != a
    monkeypatch.setattr(config, "openrouter_tts_model", "other/model")
    assert tts_router.cache_key("шаг") not in (a,)


def test_speak_validation_and_ai_error(monkeypatch):
    async def boom(text):
        raise AIError("квота")

    monkeypatch.setattr(tts_router.tts_ai, "synthesize", boom)
    with TestClient(app) as c:
        assert c.get("/api/tts", params={"text": ""}).status_code == 422
        assert c.get("/api/tts", params={"text": "   "}).status_code == 422
        assert c.get("/api/tts", params={"text": "х" * (config.tts_max_chars + 1)}).status_code == 422
        r = c.get("/api/tts", params={"text": "шаг"})
        assert r.status_code == 502 and "квота" in r.json()["detail"]
    assert not list(tts_router.cache_dir().glob("*"))  # при ошибке ничего не кэшируем


def test_concurrent_same_text_single_flight(monkeypatch):
    calls = []

    async def slow(text):
        calls.append(text)
        await asyncio.sleep(0.05)
        return b"mp3"

    monkeypatch.setattr(tts_router.tts_ai, "synthesize", slow)

    async def run():
        return await asyncio.gather(tts_router.speak("один шаг"), tts_router.speak("один шаг"))

    r1, r2 = asyncio.run(run())
    assert r1.path == r2.path and calls == ["один шаг"]


def test_synthesize_payload_and_errors(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "k")
    seen = {}

    class Resp:
        def __init__(self, status, content, ctype):
            self.status_code, self.content, self.headers = status, content, {"content-type": ctype}
            self.text = content.decode(errors="replace")

    class Client:
        reply = Resp(200, b"ID3audio", "audio/mpeg")

        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, json=json)
            return Client.reply

    monkeypatch.setattr(tts_ai.httpx, "AsyncClient", Client)
    assert asyncio.run(tts_ai.synthesize("шаг")) == b"ID3audio"
    assert seen["url"].endswith("/audio/speech")
    # Голос по умолчанию — alloy: без него Fish берёт случайного диктора на каждый шаг.
    assert seen["json"] == {"model": config.openrouter_tts_model, "input": "шаг",
                            "response_format": "mp3", "voice": "alloy"}
    monkeypatch.setattr(config, "openrouter_tts_voice", "")
    asyncio.run(tts_ai.synthesize("шаг"))
    assert "voice" not in seen["json"]  # пустой — не шлём
    monkeypatch.setattr(config, "openrouter_tts_voice", "flux-alexis-en")
    asyncio.run(tts_ai.synthesize("шаг"))
    assert seen["json"]["voice"] == "flux-alexis-en"
    # Ошибка телом JSON при 200 и не-200 → AIError.
    Client.reply = Resp(200, b'{"error":{"message":"rate limited"}}', "application/json")
    with pytest.raises(AIError):
        asyncio.run(tts_ai.synthesize("шаг"))
    Client.reply = Resp(429, b"slow down", "text/plain")
    with pytest.raises(AIError):
        asyncio.run(tts_ai.synthesize("шаг"))


def test_not_configured(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "")
    with pytest.raises(AIError):
        asyncio.run(tts_ai.synthesize("шаг"))


class _Resp429:
    status_code = 429
    content = b""
    text = ""

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self):
        return self._body


def _client_returning(resp, calls):
    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            calls.append(1)
            return resp

    return Client


DAILY = {"error": {"message": "Rate limit exceeded: free-models-per-day. Add 10 credits",
                   "code": 429, "metadata": {"limit_source": "openrouter_free_tier_daily",
                                             "headers": {"X-RateLimit-Reset": "4102444800000"}}}}


def test_daily_limit_blocks_until_reset(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "k")
    calls = []
    monkeypatch.setattr(tts_ai.httpx, "AsyncClient", _client_returning(_Resp429(DAILY), calls))
    with pytest.raises(tts_ai.TtsLimitError) as exc:
        asyncio.run(tts_ai.synthesize("шаг"))
    assert "50 запросов в сутки" in str(exc.value) and exc.value.until == 4102444800
    # Дальше — без обращения к OpenRouter, пока не наступит сброс.
    with pytest.raises(tts_ai.TtsLimitError):
        asyncio.run(tts_ai.synthesize("другой шаг"))
    assert len(calls) == 1
    ok, reason, until = tts_ai.limit_status()
    assert not ok and until == 4102444800 and "Снова заработает" in reason


def test_busy_pool_blocks_briefly(monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "k")
    body = {"error": {"message": "Provider returned error", "code": 429,
                      "metadata": {"limit_source": "upstream_provider_shared_pool"}}}
    monkeypatch.setattr(tts_ai.httpx, "AsyncClient", _client_returning(_Resp429(body), []))
    with pytest.raises(tts_ai.TtsLimitError) as exc:
        asyncio.run(tts_ai.synthesize("шаг"))
    assert "перегружен" in str(exc.value)
    assert 0 < exc.value.until - __import__("time").time() <= tts_ai._BUSY_BLOCK_SEC


def test_router_limit_is_429_with_detail_and_status(monkeypatch):
    async def limited(text):
        raise tts_ai.TtsLimitError("Бесплатная озвучка на сегодня закончилась", 4102444800)

    monkeypatch.setattr(tts_router.tts_ai, "synthesize", limited)
    with TestClient(app) as c:
        assert c.get("/api/tts/status").json() == {"available": True, "detail": "", "resetAt": None}
        r = c.get("/api/tts", params={"text": "шаг"})
        assert r.status_code == 429 and "закончилась" in r.json()["detail"]
        assert int(r.headers["retry-after"]) > 0
        monkeypatch.setattr(tts_ai, "blocked_until", 4102444800.0)
        monkeypatch.setattr(tts_ai, "blocked_reason", "лимит")
        st = c.get("/api/tts/status").json()
        assert st["available"] is False and st["detail"] == "лимит" and st["resetAt"].startswith("2100-")


def test_own_daily_limit_counts_only_successful(monkeypatch):
    from app.ai import limits

    monkeypatch.setattr(config, "tts_daily_limit", 2)
    monkeypatch.setattr(config, "openrouter_api_key", "k")
    outcomes = iter([b"a", AIError("сбой"), b"b"])

    async def fake_request(text):
        r = next(outcomes)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(tts_ai, "_request", fake_request)
    assert asyncio.run(tts_ai.synthesize("1")) == b"a"
    with pytest.raises(AIError):
        asyncio.run(tts_ai.synthesize("2"))  # сбой — лимит не тратится
    assert limits.tts_status()["used"] == 1
    assert asyncio.run(tts_ai.synthesize("3")) == b"b"
    assert limits.tts_status() == {"used": 2, "limit": 2, "remaining": 0}
    with pytest.raises(tts_ai.TtsLimitError) as exc:
        asyncio.run(tts_ai.synthesize("4"))
    assert "2 новых шагов в день" in str(exc.value)
    ok, reason, until = tts_ai.limit_status()
    assert not ok and until > __import__("time").time()


def test_cached_step_plays_when_limit_reached(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "tts_daily_limit", 1)
    monkeypatch.setattr(tts_ai, "_request", lambda t: _ok(calls, t))
    with TestClient(app) as c:
        assert c.get("/api/tts", params={"text": "первый"}).status_code == 200
        r = c.get("/api/tts", params={"text": "второй"})
        assert r.status_code == 429 and "Лимит озвучки" in r.json()["detail"]
        assert c.get("/api/tts", params={"text": "первый"}).status_code == 200  # из кэша
        lim = c.get("/api/limits").json()["tts"]
    assert calls == ["первый"] and lim == {"used": 1, "limit": 1, "remaining": 0}


async def _ok(calls, text):
    calls.append(text)
    return b"mp3"
