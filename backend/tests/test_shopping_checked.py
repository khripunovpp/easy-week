"""Отметки «куплено» — на сервере: общие для устройств, изменения не затирают чужие отметки,
старая версия плана пишет в последнюю, правка в чате переносит отметки в новую версию."""

import asyncio

from app.models import Conversation, PlanRow
from app.routers import chat as chat_router
from app.routers import plans as plans_router
from app.schemas import ChatRequest, ShoppingCheckedBody
from app.services import planstore

ST = {"vacuum": True, "freeze": True, "shelf_life_days": 60, "note": ""}


def _dish(i: str, name: str) -> dict:
    return {"id": i, "name": name, "emoji": "🍲", "servings": 4, "prep_min": 5, "cook_min": 5,
            "storage": ST}


def _put(session, pid, add=(), remove=()):
    return asyncio.run(plans_router.shopping_checked_update(
        pid, ShoppingCheckedBody(add=list(add), remove=list(remove)), session))


def test_two_devices_do_not_overwrite_each_other(session):
    session.add(Conversation(id="c"))
    planstore.new_row(session, id="p", conversation_id="c", title="П", week_label="w",
                      dishes=[_dish("a", "Суп")])
    session.commit()
    assert asyncio.run(plans_router.shopping_checked("p", session)).keys == []
    _put(session, "p", add=["лук", "хлеб"])           # устройство А
    got = _put(session, "p", add=["молоко"], remove=["лук"])  # устройство Б — со своим видом
    assert got.plan_id == "p" and got.keys == ["хлеб", "молоко"]
    assert _put(session, "p", add=["хлеб", " "]).keys == ["хлеб", "молоко"]  # без дублей/пустых
    assert _put(session, "p", remove=["хлеб", "молоко"]).keys == []
    session.expire_all()
    assert session.get(PlanRow, "p").shopping_checked is None


def test_old_version_reads_and_writes_latest_and_edit_carries_checks(session, monkeypatch):
    session.add(Conversation(id="c"))
    planstore.new_row(session, id="v1", conversation_id="c", title="П", week_label="w",
                      status="accepted", dishes=[_dish("a", "Суп"), _dish("b", "Каша")])
    session.commit()
    _put(session, "v1", add=["лук"])
    monkeypatch.setattr(chat_router.appstate, "get_current_plan", lambda: None)
    res = asyncio.run(chat_router.chat_edit(
        ChatRequest(conversation_id="c", message="", remove_dish_id="b"), session))
    v2 = res.plan.id
    session.expire_all()
    assert session.get(PlanRow, v2).shopping_checked == ["лук"]  # правка перенесла отметки
    # экран со старой версией: читает и пишет в последнюю
    got = _put(session, "v1", add=["морковь"])
    assert got.plan_id == v2 and got.keys == ["лук", "морковь"]
    assert asyncio.run(plans_router.shopping_checked("v1", session)).keys == ["лук", "морковь"]
