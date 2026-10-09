"""Свои товары в покупках (мимо рецептов): текст → позиции по отделам → в список к продуктам
рецептов; удаление; перенос в новую версию плана при правке в чате."""

import asyncio

import pytest
from fastapi import HTTPException

from app.ai import planner
from app.ai.base import AIError
from app.ai.prompt import SHOP_EXTRAS_SYSTEM, build_shop_extras_messages
from app.models import Conversation, PlanRow
from app.routers import chat as chat_router
from app.routers import plans as plans_router
from app.schemas import ChatRequest, ShoppingExtrasBody
from app.services import planstore
from app.services.shopping import CATEGORY_ORDER, clean_extras, group_items, merge_extras


class FakeGate:
    provider = "Fake"
    key = "fake"

    def __init__(self, parsed=None, error=None):
        self.parsed, self.error, self.calls = parsed, error, []

    async def complete_json(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.error:
            raise self.error
        return self.parsed, {}


def _dish(i, name, ings):
    return {
        "id": i, "name": name, "emoji": "🍲", "servings": 4, "prep_min": 10, "cook_min": 20,
        "storage": {"method": "vacuum", "shelf_life_days": 60, "note": ""},
        "ingredients": [{"name": n, "qty": q, "unit": u, "category": c} for n, q, u, c in ings],
        "steps": ["шаг"],
    }


def _seed(session, extras=None):
    session.add(Conversation(id="c1"))
    planstore.new_row(
        session, id="p1", conversation_id="c1", title="План", week_label="w", status="accepted",
        dishes=[_dish("d1", "Суп", [("Лук", 100, "г", "Овощи")]),
                _dish("d2", "Рагу", [("Говядина", 1, "кг", "Мясо и птица")])],
        shopping_extras=extras,
    )
    session.commit()


def test_prompt_lists_departments_and_keeps_user_words():
    for cat in ("Хлеб и выпечка", "Фрукты", "Напитки", "Хозтовары"):
        assert f"'{cat}'" in SHOP_EXTRAS_SYSTEM and cat in CATEGORY_ORDER
    assert "не выдумывай количество" in SHOP_EXTRAS_SYSTEM
    assert build_shop_extras_messages(" хлеб, йогурт ")[1]["content"].endswith("хлеб, йогурт")


def test_clean_extras_keeps_only_named_quantity_and_known_department():
    got = clean_extras([
        {"name": "Хлеб", "qty": 0, "unit": "шт", "category": "Хлеб и выпечка"},
        {"name": "йогурт", "qty": 2, "unit": "шт.", "category": "Молочное"},
        {"name": "молоко", "qty": 1, "unit": "пачка", "category": "Молочное"},  # не наша единица
        {"name": "губки", "qty": 1, "unit": "шт", "category": "Бытовая химия"},  # нет такого отдела
        {"name": "  ", "qty": 1, "unit": "шт", "category": "Прочее"},
        {"name": "йогурты", "qty": 1, "unit": "шт", "category": "Молочное"},  # тот же продукт
        "мусор",
    ])
    assert [(i["name"], i["qty"], i["unit"], i["category"]) for i in got] == [
        ("хлеб", 0, "", "Хлеб и выпечка"),
        ("йогурт", 3, "шт", "Молочное"),
        ("молоко", 0, "", "Молочное"),
        ("губки", 1, "шт", "Прочее"),
    ]
    assert all(i["id"] for i in got) and len({i["id"] for i in got}) == 4


def test_merge_extras_sums_and_fills_quantity():
    old = [
        {"id": "a", "name": "молоко", "qty": 1, "unit": "л", "category": "Молочное"},
        {"id": "b", "name": "хлеб", "qty": 0, "unit": "", "category": "Хлеб и выпечка"},
    ]
    new = [
        {"id": "x", "name": "молоко", "qty": 0.5, "unit": "л", "category": "Молочное"},
        {"id": "y", "name": "хлеб", "qty": 2, "unit": "шт", "category": "Хлеб и выпечка"},
        {"id": "z", "name": "молоко", "qty": 0, "unit": "", "category": "Молочное"},
        {"id": "w", "name": "бананы", "qty": 0, "unit": "", "category": "Фрукты"},
    ]
    got = merge_extras(old, new)
    assert [(i["id"], i["qty"], i["unit"]) for i in got] == [
        ("a", 1.5, "л"), ("b", 2, "шт"), ("w", 0, ""),
    ]
    assert old[0]["qty"] == 1  # исходный список не меняется


def test_group_items_puts_extras_into_departments_not_home():
    groups = group_items(
        [{"name": "лук", "qty": 100, "unit": "г", "category": "Овощи"},
         {"name": "йогурт", "qty": 200, "unit": "г", "category": "Молочное"}],
        leftovers=["йогурт"],
        extras=[{"id": "e1", "name": "йогурт", "qty": 2, "unit": "шт", "category": "Молочное"},
                {"id": "e2", "name": "бананы", "qty": 0, "unit": "", "category": "Фрукты"}],
    )
    assert [g.category for g in groups] == ["Овощи", "Фрукты", "Молочное", "Есть дома (остатки)"]
    milk = groups[2].items
    assert [(i.name, i.extra, i.id) for i in milk] == [("йогурт", True, "e1")]
    assert groups[-1].items[0].extra is False  # остаток плана — дома, свой йогурт — купить


def test_add_extras_endpoint_merges_and_get_shows_them(session, monkeypatch):
    _seed(session, extras=[{"id": "old", "name": "хлеб", "qty": 0, "unit": "",
                            "category": "Хлеб и выпечка"}])
    gate = FakeGate({"items": [
        {"name": "йогурт", "qty": 2, "unit": "шт", "category": "Молочное"},
        {"name": "хлеб", "qty": 1, "unit": "шт", "category": "Хлеб и выпечка"},
    ]})
    seen = []
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": seen.append((m, task)) or gate)
    out = asyncio.run(plans_router.shopping_extras_add(
        "p1", ShoppingExtrasBody(text="йогурт 2 шт и хлеб", recipe_model="gemini"), session
    ))
    assert seen == [("gemini", "shopping")]  # модель страницы, задача «Список покупок»
    assert "йогурт 2 шт и хлеб" in gate.calls[0][0][1]["content"]
    assert sorted((i.name, i.qty, i.unit, i.extra) for i in out) == [
        ("йогурт", 2, "шт", True), ("хлеб", 1, "шт", True),
    ]
    session.expire_all()
    assert len(session.get(PlanRow, "p1").shopping_extras) == 2

    async def no_normalize(*a, **kw):
        raise AIError("нормализатор недоступен")  # GET отдаёт базу — свои товары всё равно в ней

    monkeypatch.setattr(plans_router, "normalize_shopping", no_normalize)
    groups = asyncio.run(plans_router.shopping_list("p1", session))
    flat = {(i.name, i.extra) for g in groups for i in g.items}
    assert {("Лук", False), ("Говядина", False), ("йогурт", True), ("хлеб", True)} == flat


def test_add_extras_model_failure_is_502_and_writes_nothing(session, monkeypatch):
    _seed(session)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": FakeGate(error=AIError("429")))
    with pytest.raises(HTTPException) as e:
        asyncio.run(plans_router.shopping_extras_add(
            "p1", ShoppingExtrasBody(text="хлеб"), session
        ))
    assert e.value.status_code == 502
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": FakeGate({"items": []}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(plans_router.shopping_extras_add(
            "p1", ShoppingExtrasBody(text="хлеб"), session
        ))
    assert e.value.status_code == 502
    session.expire_all()
    assert not session.get(PlanRow, "p1").shopping_extras


def test_delete_extra(session):
    _seed(session, extras=[
        {"id": "a", "name": "хлеб", "qty": 0, "unit": "", "category": "Хлеб и выпечка"},
        {"id": "b", "name": "йогурт", "qty": 2, "unit": "шт", "category": "Молочное"},
    ])
    out = asyncio.run(plans_router.shopping_extras_delete("p1", "a", session))
    assert [i.id for i in out] == ["b"]
    assert asyncio.run(plans_router.shopping_extras_delete("p1", "b", session)) == []
    assert asyncio.run(plans_router.shopping_extras_delete("p1", "b", session)) == []
    session.expire_all()
    assert session.get(PlanRow, "p1").shopping_extras is None


def test_chat_edit_carries_extras_to_new_version(session, monkeypatch):
    extras = [{"id": "a", "name": "хлеб", "qty": 0, "unit": "", "category": "Хлеб и выпечка"}]
    _seed(session, extras=extras)
    monkeypatch.setattr(chat_router.appstate, "get_current_plan", lambda: None)
    res = asyncio.run(chat_router.chat_edit(
        ChatRequest(conversation_id="c1", message="", remove_dish_id="d1"), session
    ))
    assert res.plan.id != "p1" and [d.id for d in res.plan.dishes] == ["d2"]
    session.expire_all()
    assert session.get(PlanRow, res.plan.id).shopping_extras == extras
