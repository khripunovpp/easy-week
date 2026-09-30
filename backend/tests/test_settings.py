"""Общие настройки «Модели по умолчанию»: API GET/PUT /api/settings, атомарная запись файла,
выбор модели по задаче (gate_for(model, task)) и нормализация покупок любой моделью."""

import asyncio
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from app.ai import gates, planner
from app.ai.prompt import SHOP_SCHEMA, SHOP_SYSTEM
from app.config import settings as config
from app.main import app
from app.models import PlanRow
from app.services import regenerate
from app.services import settings as app_settings

MODELS = {"chat": "gemini", "recipe": "anthropic", "shopping": "deepseek", "cooking": "deepseek",
          "prefs": "openrouter"}


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Каждый тест — без сохранённых настроек и без пароля (settings.json во временной data/)."""
    monkeypatch.setattr(config, "app_password", "")
    app_settings._file().unlink(missing_ok=True)
    yield
    app_settings._file().unlink(missing_ok=True)


def _client() -> TestClient:
    return TestClient(app)


def test_model_keys_match_gates():
    # Literal в схеме и реестр гейтов не должны разъехаться.
    assert set(get_args(app_settings.ModelKey)) == set(gates.GATES)
    # Карта задач покрывает все задачи и ссылается только на известные модели.
    assert set(app_settings.TASK_MODELS) == set(app_settings.TASKS)
    for task, keys in app_settings.TASK_MODELS.items():
        assert keys and set(keys) <= set(gates.GATES), task
    # Дешёвые модели не предлагаем для развёрнутых рецептов и плана готовки.
    for task in ("recipe", "cooking"):
        assert "cloudflare" not in app_settings.TASK_MODELS[task]
        assert "openrouter" not in app_settings.TASK_MODELS[task]
    # Встроенные дефолты сами подчиняются карте.
    for task, key in app_settings.builtin_defaults().items():
        assert app_settings.allowed(task, key), (task, key)


def test_get_defaults_when_missing():
    with _client() as c:
        body = c.get("/api/settings").json()
    assert body["initialized"] is False
    base = config.recipe_model_default
    # Покупки по умолчанию — Cloudflare (прежнее поведение), остальное — дефолт из .env.
    assert body["models"] == {"chat": base, "recipe": base, "shopping": "cloudflare",
                              "cooking": base, "prefs": "cloudflare"}
    # Карта задач едет фронту — он строит по ней выпадашки.
    assert body["taskModels"]["recipe"] == ["deepseek", "gemini", "anthropic"]
    assert "openrouter" in body["taskModels"]["shopping"]


def test_put_persists_atomically():
    with _client() as c:
        r = c.put("/api/settings", json={"models": MODELS})
        assert r.status_code == 200
        assert r.json()["models"] == MODELS and r.json()["initialized"] is True
        assert c.get("/api/settings").json()["models"] == MODELS
    f = app_settings._file()
    assert f.exists()
    # Временных файлов после os.replace не остаётся.
    assert not list(f.parent.glob(".settings-*.tmp"))


def test_put_rejects_unknown_model_and_missing_task():
    with _client() as c:
        bad = {**MODELS, "chat": "gpt"}
        assert c.put("/api/settings", json={"models": bad}).status_code == 422
        partial = {k: v for k, v in MODELS.items() if k != "cooking"}
        assert c.put("/api/settings", json={"models": partial}).status_code == 422
        # Модель известна, но не годится для задачи (карта): Cloudflare для рецептов → 422.
        wrong = {**MODELS, "recipe": "cloudflare"}
        r = c.put("/api/settings", json={"models": wrong})
        assert r.status_code == 422 and "recipe" in r.json()["detail"]
    assert app_settings.is_initialized() is False  # ничего не записали


def test_stored_disallowed_model_falls_back_to_builtin():
    # settings.json, сохранённый до появления карты: Cloudflare на рецептах → встроенный дефолт.
    f = app_settings._file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"models": {"recipe": "cloudflare", "shopping": "openrouter"}}', encoding="utf-8")
    models = app_settings.get_models()
    assert models["recipe"] == app_settings.builtin_defaults()["recipe"]
    assert models["shopping"] == "openrouter"  # а где годится — берём как есть


def test_broken_file_falls_back_to_defaults():
    f = app_settings._file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{не json", encoding="utf-8")
    assert app_settings.get_models()["shopping"] == "cloudflare"


def test_gate_for_resolves_by_task():
    app_settings.set_models(MODELS)
    for task, key in MODELS.items():
        # Пусто и неизвестный ключ → дефолт задачи; явный выбор страницы/чата важнее.
        assert gates.gate_for("", task).key == key
        assert gates.gate_for("nope", task).key == key
        assert gates.gate_for("deepseek", task).key == "deepseek"
    assert gates.resolve_key(None, "recipe") == "anthropic"
    # Явная модель, не подходящая задаче (старый клиент / внутренний шаг) → дефолт задачи.
    assert gates.resolve_key("cloudflare", "recipe") == "anthropic"
    assert gates.resolve_key("openrouter", "cooking") == "deepseek"
    assert gates.resolve_key("cloudflare", "chat") == "cloudflare"  # для плана годится
    assert gates.gate_for("", "prefs").key == "openrouter"


class FakeGate:
    """Гейт-заглушка: запоминает kwargs вызова complete_json."""

    provider = "Fake"
    key = "fake"

    def __init__(self, parsed):
        self.parsed = parsed
        self.kw = []

    async def complete_json(self, messages, **kw):
        self.kw.append(kw)
        return self.parsed, {}


ITEMS = [{"name": "лук", "qty": 300, "unit": "г", "category": "Овощи"}]


def test_normalize_shopping_non_cloudflare_uses_json_mode(monkeypatch):
    gate = FakeGate({"items": ITEMS})
    seen = []
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": seen.append((m, task)) or gate)
    out = asyncio.run(planner.normalize_shopping(ITEMS, model="gemini"))
    assert out == ITEMS and seen == [("gemini", "shopping")]
    # Не Cloudflare — без json_schema и без CF-модели: форма ответа описана в промпте.
    assert "schema" not in gate.kw[0] and "model" not in gate.kw[0]
    assert '"items"' in SHOP_SYSTEM


def test_normalize_shopping_cloudflare_passes_schema(monkeypatch):
    gate = FakeGate({"items": ITEMS})
    monkeypatch.setattr(planner, "cloudflare", gate)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    asyncio.run(planner.normalize_shopping(ITEMS))
    assert gate.kw[0]["schema"] is SHOP_SCHEMA and gate.kw[0]["model"] == config.cf_model_judge


def test_normalize_shopping_empty_is_ai_error(monkeypatch):
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": FakeGate({"items": []}))
    with pytest.raises(planner.AIError):
        asyncio.run(planner.normalize_shopping(ITEMS, model="deepseek"))


def test_regenerate_shopping_backfill_uses_recipe_default(session, monkeypatch):
    # Блюдо без ингредиентов → бэкфилл рецепта; нормализация — моделью со страницы покупок.
    session.add(PlanRow(id="p1", conversation_id="", title="План", week_label="1–7", dishes=[{
        "id": "d1", "name": "Борщ", "emoji": "🥣", "servings": 4, "prep_min": 5,
        "cook_min": 40, "storage": {"shelf_life_days": 60},
    }]))
    session.commit()
    detail_models, norm_models = [], []

    async def fake_detail(name, servings=4, change="", model="", **kw):
        detail_models.append(model)
        return {"ingredients": ITEMS, "steps": [], "tips": [], "note": "", "provider": "X"}

    async def fake_norm(items, discussion="", model=""):
        norm_models.append(model)
        return items

    monkeypatch.setattr(regenerate, "generate_dish_detail", fake_detail)
    monkeypatch.setattr(regenerate, "normalize_shopping", fake_norm)
    asyncio.run(regenerate.regenerate_shopping(session, session.get(PlanRow, "p1"), "gemini"))
    # Бэкфилл — без модели (планер возьмёт дефолт «Рецепты»), нормализация — выбранной.
    assert detail_models == [""] and norm_models == ["gemini"]
