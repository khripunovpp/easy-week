"""Выбор конкретной модели провайдера в настройках: ссылки «провайдер:id», выбор гейта,
валидация API, особенности гейтов для новых моделей, цены по конкретной модели."""

import pytest
from fastapi.testclient import TestClient

from app.ai import anthropic as claude_mod
from app.ai import deepseek as ds_mod
from app.ai import gates
from app.ai import gemini as gm_mod
from app.ai.cloudflare import CloudflareGate
from app.config import settings as config
from app.main import app
from app.services import model_catalog, prices
from app.services import settings as app_settings

BASE = {"chat": "deepseek", "recipe": "anthropic", "shopping": "cloudflare", "cooking": "deepseek",
        "prefs": "cloudflare", "summary": "cloudflare"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "app_password", "")
    app_settings._file().unlink(missing_ok=True)
    prices._path().unlink(missing_ok=True)
    yield
    app_settings._file().unlink(missing_ok=True)
    prices._path().unlink(missing_ok=True)


def test_refs_split_and_normalize():
    assert model_catalog.split_ref("openrouter:nvidia/nemotron-3-super-120b-a12b:free") == (
        "openrouter", "nvidia/nemotron-3-super-120b-a12b:free")
    assert model_catalog.split_ref("Anthropic") == ("anthropic", "")
    # модель по умолчанию из .env — без id (переживёт смену .env)
    assert model_catalog.make_ref("anthropic", config.anthropic_model) == "anthropic"
    assert model_catalog.models_for("anthropic")[0]["id"] == config.anthropic_model


def test_gate_for_uses_concrete_model_from_settings():
    app_settings.set_models({**BASE, "recipe": "anthropic:claude-sonnet-5-5"})
    g = gates.gate_for("", "recipe")
    assert g.key == "anthropic" and g.default_model == "claude-sonnet-5-5"
    # Страница выбрала провайдера «Claude» — та же модель, что в настройках задачи.
    assert gates.gate_for("anthropic", "recipe").default_model == "claude-sonnet-5-5"
    # Для задачи с другим провайдером — модель Claude по умолчанию (.env).
    assert gates.gate_for("anthropic", "chat").default_model == config.anthropic_model
    # Явная конкретная модель важнее; неизвестная — модель задачи/провайдера.
    assert gates.gate_for(f"anthropic:{config.anthropic_model}", "recipe").default_model == config.anthropic_model
    assert gates.gate_for("anthropic:gpt-5", "recipe").default_model == "claude-sonnet-5-5"
    # Модульный синглтон не меняется — копия.
    assert gates.anthropic.default_model == config.anthropic_model


def test_settings_api_validates_refs_and_returns_catalog():
    with TestClient(app) as c:
        ok = {**BASE, "chat": "anthropic:claude-sonnet-5-5", "recipe": f"anthropic:{config.anthropic_model}"}
        r = c.put("/api/settings", json={"models": ok})
        assert r.status_code == 200
        body = r.json()
        assert body["models"]["chat"] == "anthropic:claude-sonnet-5-5"
        assert body["models"]["recipe"] == "anthropic"  # модель по умолчанию — без id
        assert any(m["id"] == "claude-sonnet-5-5" for m in body["catalog"]["anthropic"])
        assert c.put("/api/settings", json={"models": {**BASE, "chat": "anthropic:gpt-5"}}).status_code == 422
        # Провайдер не годится для задачи — 422, даже с конкретной моделью.
        bad = {**BASE, "recipe": "openrouter:nvidia/nemotron-3-super-120b-a12b:free"}
        assert c.put("/api/settings", json={"models": bad}).status_code == 422


def test_stored_model_missing_from_catalog_keeps_provider():
    f = app_settings._file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"models": {"chat": "anthropic:claude-3-opus", "recipe": "gemini:gemini-flash-lite-latest"}}',
                 encoding="utf-8")
    m = app_settings.get_models()
    assert m["chat"] == "anthropic" and m["recipe"] == "gemini:gemini-flash-lite-latest"


def test_cloudflare_override_changes_main_not_specs():
    g = CloudflareGate().with_model("@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    assert g.main_model == g.menu_model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert g.default_model == config.cf_model  # спеки пайплайна — прежняя быстрая модель
    assert gates.is_cloudflare(g) and gates.cf_main(CloudflareGate()) == config.cf_model_judge


def test_gate_quirks_for_new_models():
    assert ds_mod._no_thinking("deepseek-chat") == {}
    assert ds_mod._no_thinking("deepseek-v4-pro") == {"thinking": {"type": "disabled"}}
    assert "thinkingConfig" not in gm_mod._gen_config(0.7, 100, "gemini-flash-lite-latest")
    assert gm_mod._gen_config(0.7, 100, "gemini-flash-latest")["thinkingConfig"] == {"thinkingBudget": 0}
    assert claude_mod._thinking_kw("claude-haiku-4-5", 3000) == {"max_tokens": 3000}
    kw = claude_mod._thinking_kw("claude-sonnet-5-5", 3000)
    assert kw["output_config"] == {"effort": "low"} and kw["max_tokens"] > 3000
    assert not claude_mod._supports_prefill("claude-sonnet-5-5") and claude_mod._supports_prefill("claude-haiku-4-5")


def test_cost_uses_model_price_then_provider():
    u = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert prices.cost_usd("anthropic", u, "claude-sonnet-5-5") == pytest.approx(12.0)
    assert prices.cost_usd("anthropic", u, "claude-haiku-4-5") == pytest.approx(6.0)
    # модели без своей строки — по цене провайдера
    assert prices.cost_usd("deepseek", u, "deepseek-v4-pro") == pytest.approx(
        prices.cost_usd("deepseek", u))
    with TestClient(app) as c:
        row = {"input": 1, "cachedInput": 0.1, "cacheWrite": 1, "output": 2}
        assert c.put("/api/settings/prices", json={"prices": {"deepseek:deepseek-v4-pro": row}}).status_code == 200
        assert c.put("/api/settings/prices", json={"prices": {"deepseek:gpt": row}}).status_code == 422
    assert prices.cost_usd("deepseek", u, "deepseek-v4-pro") == pytest.approx(3.0)
