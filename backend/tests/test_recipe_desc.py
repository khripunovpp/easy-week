"""Описание блюда принадлежит варианту рецепта: ↻ «без болгарского перца» меняет и описание,
выбор другой модели показывает её описание, у старых вариантов — задумка из плана."""

import asyncio

from app.ai import planner
from app.ai.prompt import DISH_DETAIL_SYSTEM, build_dish_detail_messages
from app.services.mapping import to_dish
from app.services.variants import active_desc, apply_variant, with_detail

PLAN_DESC = "Прозрачный бульон с морковью и болгарским перцем, фрикадельки из говядины с рисом."
BASE = {"id": "soup", "name": "Суп с фрикадельками", "emoji": "🍲", "servings": 4,
        "prep_min": 10, "cook_min": 40, "desc": PLAN_DESC,
        "storage": {"freeze": True, "shelf_life_days": 60, "note": ""}}


def _detail(desc: str, model: str = "anthropic") -> dict:
    return {"ingredients": [{"name": "говядина", "qty": 400, "unit": "г",
                             "category": "Мясо и птица"}],
            "steps": ["варить"], "tips": [], "note": "", "desc": desc, "provider": model.title(),
            "model": model, "model_ref": f"{model}:m", "gen_id": "g"}


def test_prompt_asks_desc_of_this_recipe():
    assert '"desc"' in DISH_DETAIL_SYSTEM and "по ЭТОМУ рецепту" in DISH_DETAIL_SYSTEM
    assert "убранного продукта в desc нет" in DISH_DETAIL_SYSTEM


def test_desc_follows_active_variant_and_falls_back_to_plan():
    assert to_dish(BASE).desc == PLAN_DESC  # рецепта ещё нет — задумка плана
    no_pepper = "Прозрачный бульон с морковью и фрикадельками из говядины с рисом."
    d = with_detail(BASE, "anthropic", _detail(no_pepper), kind="regenerate",
                    change="без болгарского перца")
    assert d["desc"] == PLAN_DESC  # задумка плана не меняется
    assert d["variants"]["anthropic"]["desc"] == no_pepper
    assert to_dish(d).desc == no_pepper and active_desc(d) == no_pepper
    d = with_detail(d, "deepseek", _detail("Суп-лапша с фрикадельками.", "deepseek"),
                    kind="generate")
    assert to_dish(d).desc == "Суп-лапша с фрикадельками."
    d = apply_variant(d, "anthropic", d["variants"])  # выбрали прежний вариант — его описание
    assert to_dish(d).desc == no_pepper
    old = with_detail(BASE, "gemini", {**_detail(""), "model": "gemini"}, kind="backfill")
    assert to_dish(old).desc == PLAN_DESC  # вариант без описания — задумка плана


def test_next_generation_sees_desc_of_active_recipe():
    d = with_detail(BASE, "anthropic", _detail("Бульон с фрикадельками, без перца."),
                    kind="regenerate", change="без болгарского перца")
    user = build_dish_detail_messages(d["name"], 4, dish=d)[1]["content"]
    assert "Задумка блюда (рецепт ей соответствует): Бульон с фрикадельками, без перца." in user
    assert "болгарским перцем" not in user


def test_generate_dish_detail_returns_clipped_desc(monkeypatch):
    class Gate:
        provider, key, configured = "Fake", "deepseek", True

        async def complete_json(self, messages, **kw):
            return {"desc": "  Суп   с фрикадельками. ", "ingredients": [
                {"name": "говядина", "qty": 400, "unit": "г", "category": "Мясо и птица"}],
                "steps": ["варить"], "tips": [], "note": ""}, {}

    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": Gate())
    monkeypatch.setattr(planner, "enforce_daily", lambda *a, **kw: None)
    det = asyncio.run(planner.generate_dish_detail("Суп", 4, model="deepseek"))
    assert det["desc"] == "Суп с фрикадельками."
