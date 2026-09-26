"""Версии плана после правки: «текущий» план идёт к последней версии, список прячет заменённые."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.models import Conversation, PlanRow
from app.routers import chat as chat_router
from app.routers import plans as plans_router


def _plan(pid: str, parent: str | None, status: str, minutes: int) -> PlanRow:
    return PlanRow(
        id=pid,
        conversation_id="c1",
        title=pid,
        week_label="w",
        status=status,
        parent_id=parent,
        dishes=[],
        created_at=datetime(2026, 9, 26, tzinfo=timezone.utc) + timedelta(minutes=minutes),
    )


def _seed(session):
    session.add(Conversation(id="c1"))
    # v1 (принят) → v2 (правка) → v3 (ещё правка); v1 и v2 авто-«отклонены» правкой.
    session.add(_plan("v1", None, "rejected", 0))
    session.add(_plan("v2", "v1", "rejected", 1))
    session.add(_plan("v3", "v2", "accepted", 2))
    session.add(_plan("other", None, "accepted", 3))
    session.commit()


def test_latest_version_follows_chain(session):
    _seed(session)
    assert chat_router._latest_version(session, "v1") == "v3"
    assert chat_router._latest_version(session, "v3") == "v3"
    assert chat_router._latest_version(session, "other") == "other"


def test_current_plan_moves_to_latest(session, monkeypatch):
    _seed(session)
    state = {"pid": "v1"}
    monkeypatch.setattr(chat_router.appstate, "get_current_plan", lambda: state["pid"])
    monkeypatch.setattr(chat_router.appstate, "set_current_plan", lambda p: state.update(pid=p))
    got = asyncio.run(chat_router.get_current_plan(session))
    assert got == {"planId": "v3"}
    assert state["pid"] == "v3"  # указатель переписан — покупки/готовка берут новую версию


def test_list_hides_superseded_versions(session):
    _seed(session)
    ids = [p.id for p in asyncio.run(plans_router.list_plans(session))]
    assert ids == ["other", "v3"]


def test_shopping_by_dish_groups_per_dish(session):
    """«По рецептам»: у каждого блюда свои позиции, одинаковые формы продукта слиты."""
    session.add(Conversation(id="c2"))
    ing = lambda n, q, u, c: {"name": n, "qty": q, "unit": u, "category": c}  # noqa: E731
    st = {"method": "vacuum", "shelf_life_days": 60, "note": ""}
    dish = lambda i, name, ings: {  # noqa: E731
        "id": i, "name": name, "emoji": "🍲", "servings": 4, "prep_min": 10, "cook_min": 20,
        "storage": st, "ingredients": ings, "steps": ["шаг"],
    }
    session.add(PlanRow(
        id="p", conversation_id="c2", title="t", week_label="w", dishes=[
            dish("d1", "Суп", [ing("Лук", 100, "г", "Овощи"), ing("лук", 50, "г", "Овощи")]),
            dish("d2", "Рагу", [ing("Говядина", 1, "кг", "Мясо и птица")]),
        ],
    ))
    session.commit()
    got = asyncio.run(plans_router.shopping_by_dish("p", session))
    assert [(d.dish_id, [(i.name, i.qty, i.unit) for i in d.items]) for d in got] == [
        ("d1", [("Лук", 150, "г")]),
        ("d2", [("Говядина", 1, "кг")]),
    ]
