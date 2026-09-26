"""Учёт затрат: стоимость по usage и текущей таблице цен, правка цен, метрика."""

import pytest

from app.ai import observe
from app.services import prices


@pytest.fixture(autouse=True)
def _tmp_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(prices, "_path", lambda: tmp_path / "prices.json")


def test_cost_deepseek_with_cache():
    # 1000 промпта, из них 400 из кэша; 500 вывода — по дефолтным ценам DeepSeek
    u = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 500}
    p = prices.DEFAULT_PRICES["deepseek"]
    want = (600 * p["input"] + 400 * p["cached_input"] + 500 * p["output"]) / 1e6
    assert prices.cost_usd("deepseek", u) == pytest.approx(want)


def test_cost_claude_cache_write_and_cloudflare_neurons():
    u = {"prompt_tokens": 5000, "prompt_cache_write_tokens": 4500, "completion_tokens": 100}
    p = prices.DEFAULT_PRICES["anthropic"]
    want = (500 * p["input"] + 4500 * p["cache_write"] + 100 * p["output"]) / 1e6
    assert prices.cost_usd("anthropic", u) == pytest.approx(want)
    assert prices.cost_usd("cloudflare", {"neurons": 1000, "prompt_tokens": 9}) == pytest.approx(0.011)


def test_saved_price_applies_to_new_calls():
    prices.save({"deepseek": {"input": 1.0, "cached_input": 0.0, "cache_write": 1.0, "output": 0.0}})
    assert prices.load()["deepseek"]["input"] == 1.0
    assert prices.cost_usd("deepseek", {"prompt_tokens": 1_000_000}) == pytest.approx(1.0)
    assert prices.load()["gemini"] == prices.DEFAULT_PRICES["gemini"]  # остальные не тронуты


def test_cloudflare_cached_tokens_normalized_and_cost_recorded():
    u = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}, "neurons": 50}
    assert observe._norm_cache(u)["prompt_cache_hit_tokens"] == 40
    cost = observe._record_metrics("Cloudflare", "m", "список покупок", u)
    assert cost == pytest.approx(50 / 1000 * 0.011)
