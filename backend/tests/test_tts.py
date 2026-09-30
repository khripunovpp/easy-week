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
        assert r.content == b"ID3fake" and "max-age" in r.headers["cache-control"]
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
    assert seen["json"] == {"model": config.openrouter_tts_model, "input": "шаг", "response_format": "mp3"}
    # Голос передаём только если задан (Deepgram Flux требует, Fish — нет).
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
