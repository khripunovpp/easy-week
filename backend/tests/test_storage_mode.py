"""Режим хранения блюда: по умолчанию заготовка под заморозку (❄️), «свежее» (🌿) — только
по просьбе пользователя; компоненты «в день подачи» помечаются в рецепте (ингредиент fresh)."""

import asyncio

from app.ai import planner
from app.ai.prompt import (
    COOKPLAN_SYSTEM,
    CUSTOM_RECIPE_SYSTEM,
    DEEPSEEK_PLAN_SYSTEM,
    DISH_DETAIL_SYSTEM,
    SINGLE_DISH_SYSTEM,
    build_cook_plan_messages,
    build_dish_detail_messages,
    discuss_recipe_context,
)
from app.schemas import Dish, Ingredient


class FakeGate:
    provider = "Fake"
    key = "fake"

    def __init__(self, parsed):
        self.parsed = parsed

    async def complete_json(self, messages, **kw):
        return self.parsed, {}


def test_storage_defaults_to_freeze():
    s = planner._clean_storage(None)
    assert s["freeze"] is True and s["vacuum"] is True and s["shelf_life_days"] == 45
    # непонятное значение freeze — тоже заготовка; срок морозилки зажат 7–180
    s = planner._clean_storage({"freeze": None, "shelf_life_days": 400})
    assert s["freeze"] is True and s["shelf_life_days"] == 180
    s = planner._clean_storage({"freeze": True, "shelf_life_days": "abc"}, default_days=30)
    assert s["shelf_life_days"] == 30


def test_storage_fresh_only_explicit():
    s = planner._clean_storage({"freeze": False, "shelf_life_days": 0})
    assert s["freeze"] is False and s["vacuum"] is False and s["shelf_life_days"] == 0
    # «false» строкой — тоже свежее; дни холодильника зажаты 0–5
    s = planner._clean_storage({"freeze": "false", "shelf_life_days": 60})
    assert s["freeze"] is False and s["shelf_life_days"] == 5
    s = planner._clean_storage({"freeze": False})
    assert s["shelf_life_days"] == 1


def test_clean_dish_keeps_fresh_mode():
    d = planner._clean_dish(0, {"name": "Салат из огурцов", "emoji": "🥗",
                                "storage": {"freeze": False, "shelf_life_days": 0}})
    assert d["storage"]["freeze"] is False
    assert Dish.model_validate({**d, "id": "x"}).storage.freeze is False


def test_detail_fresh_flag_only_explicit_true():
    parsed = {"ingredients": [
        {"name": "спагетти", "qty": 300, "unit": "г", "category": "Бакалея", "fresh": True},
        {"name": "петрушка", "qty": 10, "unit": "г", "category": "Овощи", "fresh": "true"},
        {"name": "фарш", "qty": 500, "unit": "г", "category": "Мясо и птица", "fresh": None},
        {"name": "томаты", "qty": 400, "unit": "г", "category": "Овощи", "fresh": False},
    ], "steps": ["a"], "tips": [], "note": "Морозилка: соус\nВ день подачи: отвари спагетти"}
    det = planner._clean_detail(parsed, FakeGate({}))
    assert [i.get("fresh") for i in det["ingredients"]] == [True, True, None, None]
    assert "fresh" not in det["ingredients"][2] and "fresh" not in det["ingredients"][3]
    assert det["note"].startswith("Морозилка:")


def test_ingredient_schema_fresh():
    base = {"name": "укроп", "qty": 5, "unit": "г", "category": "Овощи"}
    assert Ingredient.model_validate(base).fresh is False
    assert Ingredient.model_validate({**base, "fresh": None}).fresh is False
    assert Ingredient.model_validate({**base, "fresh": True}).fresh is True
    assert Ingredient.model_validate(base).model_dump(by_alias=True)["fresh"] is False


def test_prompts_mode_rules():
    for system in (DEEPSEEK_PLAN_SYSTEM, SINGLE_DISH_SYSTEM):
        assert "РЕЖИМ БЛЮДА" in system and '"freeze": true' in system
        assert "ТОЛЬКО если пользователь" in system
    # правила заготовки — в общем префиксе: рыба в день подачи, крупы, Е1422, свежее
    for system in (DEEPSEEK_PLAN_SYSTEM, DISH_DETAIL_SYSTEM, COOKPLAN_SYSTEM):
        assert "готовую рыбу НИКОГДА не морозь" in system and "Е1422" in system and "КРУПЫ" in system
        assert "суббот" not in system.lower()
    for system in (DISH_DETAIL_SYSTEM, CUSTOM_RECIPE_SYSTEM):
        assert '"fresh": true' in system and "«В день подачи:»" in system
    assert '"freeze": true' in CUSTOM_RECIPE_SYSTEM
    assert "«В день подачи»" in COOKPLAN_SYSTEM


def test_detail_header_carries_mode():
    fresh = {"storage": {"freeze": False, "shelf_life_days": 0}}
    user = build_dish_detail_messages("Салат", 2, dish=fresh)[1]["content"]
    assert "режим: СВЕЖЕЕ — не замораживать, едят сразу" in user
    frozen = {"storage": {"freeze": True, "shelf_life_days": 60}}
    user = build_dish_detail_messages("Гуляш", 4, dish=frozen)[1]["content"]
    assert "режим: заготовка под заморозку (до 60 дн)" in user
    # старые планы без storage — заготовка
    user = build_dish_detail_messages("Гуляш", 4, dish={"tags": ["мясо"]})[1]["content"]
    assert "режим: заготовка под заморозку" in user


def test_cook_plan_marks_fresh():
    dishes = [
        {"name": "Болоньезе", "servings": 4, "storage": {"freeze": True},
         "ingredients": [{"name": "спагетти", "qty": 400, "unit": "г", "fresh": True},
                         {"name": "фарш", "qty": 500, "unit": "г"}]},
        {"name": "Салат", "servings": 2, "storage": {"freeze": False, "shelf_life_days": 0},
         "ingredients": [{"name": "огурцы", "qty": 300, "unit": "г"}]},
    ]
    user = build_cook_plan_messages(dishes)[1]["content"]
    assert "спагетти 400г (в день подачи); фарш 500г\n" in user
    assert "[2] Салат (2 порц.) — СВЕЖЕЕ" in user
    assert "[1] Болоньезе (4 порц.)\n" in user


def test_discuss_context_marks_fresh():
    dish = {"name": "Болоньезе", "servings": 4, "storage": {"freeze": True, "shelf_life_days": 60},
            "ingredients": [{"name": "спагетти", "qty": 400, "unit": "г", "fresh": True}]}
    ctx = discuss_recipe_context(dish, [])
    assert "спагетти 400 г (в день подачи)" in ctx and "режим: заготовка" in ctx


def test_custom_recipe_fresh_mode(monkeypatch):
    gate = FakeGate({
        "name": "Салат", "emoji": "🥗", "servings": 2, "prep_min": 10, "cook_min": 0,
        "tags": [], "freeze": False, "shelf_life_days": 0,
        "ingredients": [{"name": "огурцы", "qty": 300, "unit": "г", "category": "Овощи"}],
        "steps": ["нарежь"], "tips": [], "note": "",
    })
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    head, _, _ = asyncio.run(planner.generate_custom_recipe("салат из огурцов"))
    assert head["storage"]["freeze"] is False and head["storage"]["shelf_life_days"] == 0


def test_cook_plan_fresh_fridge_dish_is_cooked_ahead():
    # «На 2–3 дня свежим» — готовим заранее и в холодильник, а не в день подачи
    dishes = [{"name": "Гуляш", "servings": 4, "storage": {"freeze": False, "shelf_life_days": 3},
               "ingredients": []}]
    user = build_cook_plan_messages(dishes)[1]["content"]
    assert "СВЕЖЕЕ: готовится заранее, хранится в холодильнике до 3 дн" in user
    assert "готовь в заготовке как обычно, но без заморозки" in COOKPLAN_SYSTEM


def test_note_list_or_dict_does_not_crash():
    base = {"ingredients": [], "steps": ["a"], "tips": []}
    det = planner._clean_detail({**base, "note": ["Морозилка: соус", " ", "Разогрев: до кипения"]},
                                FakeGate({}))
    assert det["note"] == "Морозилка: соус\nРазогрев: до кипения"
    det = planner._clean_detail({**base, "note": {"Морозилка": "соус", "Важно": ""}}, FakeGate({}))
    assert det["note"] == "Морозилка: соус"
    det = planner._clean_detail({**base, "note": None, "ingredients": "говядина 500 г"},
                                FakeGate({}))
    assert det["note"] == "" and det["ingredients"] == []


def test_book_recipe_only_for_same_mode():
    from app.services import recipebook

    book = {"name": "Салат Цезарь", "steps": ["нарежь"], "ingredients": [{"name": "салат"}],
            "variants": {"deepseek": {"steps": ["нарежь"], "ingredients": []}},
            "storage": {"freeze": True, "shelf_life_days": 45}}
    index = {"салат цезарь": book}
    fresh = {"id": "d1", "name": "Салат Цезарь", "storage": {"freeze": False, "shelf_life_days": 0}}
    assert recipebook.attach(fresh, index) is fresh  # просили «на сегодня» — рецепт под заморозку не берём
    frozen = {"id": "d2", "name": "Салат Цезарь", "storage": {"freeze": True, "shelf_life_days": 30}}
    assert recipebook.attach(frozen, index)["from_book"] is True
    own = {**book, "source": "мой цезарь"}  # свой рецепт — всегда, режим задаёт он
    got = recipebook.attach(fresh, {"салат цезарь": own})
    assert got["from_book"] is True and got["storage"]["freeze"] is True


def test_cloudflare_fresh_mode_comes_from_menu(monkeypatch):
    from app.ai import gates

    async def cf_complete(messages, schema=None, **kw):
        label = kw.get("label", "")
        if label == "меню":
            return {"reply": "ок", "title": "План", "dishes": [
                {"name": "Гуляш", "emoji": "🍲"},
                {"name": "Салат из огурцов", "emoji": "🥗", "fresh": True, "fresh_days": 0},
            ]}, {}
        if label == "валидатор блюд":
            return {"results": []}, {}
        # спекер блюда: даже если 8b решил «свежее» — режим не его
        return {"servings": 4, "prep_min": 10, "cook_min": 30, "tags": [], "ingredients": [],
                "storage": {"vacuum": True, "freeze": False, "shelf_life_days": 2, "note": "н"}}, {}

    monkeypatch.setattr(gates.cloudflare, "complete_json", cf_complete)
    res = asyncio.run(planner._generate_plan_cloudflare("ужины, а сегодня салат", [], 2))
    goulash, salad = res["dishes"]
    assert goulash["storage"]["freeze"] is True and goulash["storage"]["shelf_life_days"] == 30
    assert salad["storage"]["freeze"] is False and salad["storage"]["shelf_life_days"] == 0
    assert salad["storage"]["note"] == "н"
