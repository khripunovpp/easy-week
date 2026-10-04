"""Свой рецепт: «Улучшить» → «Дальше» → «Мои рецепты» (служебный план library) → книга рецептов."""

import asyncio

from app.ai import planner
from app.ai.prompt import (
    CUSTOM_RECIPE_SYSTEM,
    DISH_DETAIL_SYSTEM,
    IMPROVE_RECIPE_SYSTEM,
    build_dish_detail_messages,
    build_single_dish_messages,
)
from app.routers import plans as plans_router
from app.routers import recipes as recipes_router
from app.schemas import RecipeTextBody
from app.services import recipebook

TEXT = "сырники: творог 500г, яйцо 1, мука 3 ложки, сахар. жарю на сковородке до корочки"


class FakeGate:
    provider = "Fake"
    key = "fake"
    supports_stream = False
    supports_tools = False

    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    async def complete_json(self, messages, **kw):
        self.calls.append((messages, kw))
        return self.parsed, {}


RECIPE = {
    "name": "Сырники", "emoji": "🥞", "desc": "Творожные оладьи, обжаренные до корочки.",
    "servings": 4, "prep_min": 15, "cook_min": 20, "tags": ["завтрак"], "shelf_life_days": 60,
    "ingredients": [{"name": "творог", "qty": 500, "unit": "г", "category": "Молочное"}],
    "steps": ["смешай", "сформуй", "обжарь"], "tips": ["совет"], "note": "разогрей",
}


def test_prompts_keep_user_recipe_and_share_rules():
    assert "НИЧЕГО не придумывай" in IMPROVE_RECIPE_SYSTEM and '{"text"' in IMPROVE_RECIPE_SYSTEM
    assert "СТРОГО по нему" in CUSTOM_RECIPE_SYSTEM and '"shelf_life_days"' in CUSTOM_RECIPE_SYSTEM
    # общие правила единиц/шагов — те же, что у рецепта блюда плана
    assert "ЕДИНИЦЫ ингредиентов" in CUSTOM_RECIPE_SYSTEM and "ЕДИНИЦЫ ингредиентов" in DISH_DETAIL_SYSTEM
    user = build_dish_detail_messages("Сырники", 4, dish={"source": TEXT})[1]["content"]
    assert "Это СВОЙ рецепт пользователя" in user and "творог 500г" in user
    single = build_single_dish_messages("мои сырники", book=["Сырники"])[1]["content"]
    assert "Книга рецептов семьи" in single and "Сырники" in single


def test_improve_returns_text(monkeypatch):
    gate = FakeGate({"text": "Сырники\nСостав:\n- творог — 500 г"})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    out = asyncio.run(recipes_router.improve_recipe(RecipeTextBody(text=TEXT)))
    assert out.text.startswith("Сырники") and gate.calls[0][0][1]["content"] == TEXT


def test_custom_recipe_goes_to_library_and_book(session, monkeypatch):
    gate = FakeGate(RECIPE)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    out = asyncio.run(recipes_router.create_custom_recipe(RecipeTextBody(text=TEXT), session))
    assert out.plan_id == recipebook.LIBRARY_ID
    lib = recipebook.library_row(session)
    dish = lib.dishes[0]
    assert dish["id"] == out.dish_id and dish["id"].startswith("own-0-")
    assert dish["name"] == "Сырники" and dish["source"] == TEXT and dish["desc"]
    assert dish["active_model"] == "fake" and dish["steps"] == ["смешай", "сформуй", "обжарь"]
    assert dish["storage"]["shelf_life_days"] == 60 and dish["storage"]["note"] == "разогрей"
    # второй свой рецепт с тем же названием — другой id
    out2 = asyncio.run(recipes_router.create_custom_recipe(RecipeTextBody(text=TEXT), session))
    assert out2.dish_id != out.dish_id
    # в книге рецептов (план из чата возьмёт готовый рецепт вместе с source)
    assert "Сырники" in recipebook.book_names(session)
    got = recipebook.attach({"id": "dish-1", "name": "Сырники", "emoji": "🥞"},
                            recipebook.book_index(session))
    assert got["from_book"] and got["source"] == TEXT
    # в «Рецептах» — первыми, в списке планов — нет
    items = asyncio.run(recipes_router.list_recipes(session))
    assert items[0].plan_id == recipebook.LIBRARY_ID and items[0].has_recipe
    assert all(p.id != recipebook.LIBRARY_ID for p in asyncio.run(plans_router.list_plans(session)))


def test_custom_recipe_without_steps_is_error(session, monkeypatch):
    gate = FakeGate({**RECIPE, "steps": []})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    try:
        asyncio.run(recipes_router.create_custom_recipe(RecipeTextBody(text=TEXT), session))
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 502
    else:
        raise AssertionError("ожидали 502")
    assert session.get(recipebook.PlanRow, recipebook.LIBRARY_ID) is None  # ничего не создали


def test_recipe_list_has_generated_at(session, monkeypatch):
    """«По дате» в Рецептах — дата генерации рецепта (detail_generated_at) в RecipeItem."""
    gate = FakeGate(RECIPE)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    asyncio.run(recipes_router.create_custom_recipe(RecipeTextBody(text=TEXT), session))
    item = asyncio.run(recipes_router.list_recipes(session))[0]
    assert item.generated_at is not None and item.generated_at.tzinfo is not None
    assert recipes_router._iso("bad") is None and recipes_router._iso("") is None


def test_regenerate_note_is_change_and_sticks_to_own_recipe(session, monkeypatch):
    """«Что учесть?» перед ↻: уточнение — обязательная правка; у своего рецепта — в source."""
    from app.routers.plans import dish_details
    from app.schemas import DetailRequest

    gate = FakeGate(RECIPE)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    out = asyncio.run(recipes_router.create_custom_recipe(RecipeTextBody(text=TEXT), session))
    monkeypatch.setattr("app.routers.plans.gate_for", lambda m, task="chat": gate)
    monkeypatch.setattr("app.services.regenerate.gate_for", lambda m, task="chat": gate)
    req = DetailRequest(recipe_model="fake", action="regenerate", note="соус на сливках")
    asyncio.run(dish_details(out.plan_id, out.dish_id, req, session))
    user = gate.calls[-1][0][1]["content"]
    assert "Изменение рецепта (обязательно учти): соус на сливках" in user
    assert "изменение рецепта ниже" in user  # правило перегенерации не требует «заметно другого»
    dish = recipebook.library_row(session).dishes[0]
    assert dish["source"].endswith("Уточнение: соус на сливках")
    # ↻ с уточнением — в метаданных варианта: kind regenerate + change, от прежнего варианта
    v = dish["variants"]["fake"]
    assert v["kind"] == "regenerate" and v["change"] == "соус на сливках"
    assert v["parent_id"] == "fake" and v["gen_id"]


def test_custom_head_has_no_body_and_variant_is_custom(session, monkeypatch):
    """Шапка своего рецепта — без тела (ингредиенты/шаги/советы живут только в детали →
    варианте); вариант помечен kind custom, с точной моделью и gen_id."""
    gate = FakeGate(RECIPE)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    head, detail, key = asyncio.run(planner.generate_custom_recipe(TEXT))
    assert not {"ingredients", "steps", "tips"} & set(head)
    assert head["name"] == "Сырники" and head["desc"] and head["storage"]["shelf_life_days"] == 60
    assert key == detail["model"] == "fake" and detail["model_ref"] == "fake"
    assert detail["steps"] == RECIPE["steps"] and len(detail["gen_id"]) == 32
    out = asyncio.run(recipes_router.create_custom_recipe(RecipeTextBody(text=TEXT), session))
    dish = recipebook.library_row(session).dishes[0]
    assert dish["id"] == out.dish_id and dish["steps"] == RECIPE["steps"]  # плоские = вариант
    v = dish["variants"]["fake"]
    assert v["kind"] == "custom" and v["change"] == "" and v["parent_id"] is None
    assert v["ctx_uses"] == [] and v["model_ref"] == "fake" and v["gen_id"]
