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
