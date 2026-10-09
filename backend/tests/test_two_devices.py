"""Два устройства на одном плане: одинаковые запросы склеиваются в один вызов модели, правка
блюда в старой версии плана переезжает в новые (там, где блюдо не трогали)."""

import asyncio

from sqlmodel import Session

from app.models import Conversation, PlanRow
from app.routers import plans as plans_router
from app.schemas import DetailRequest
from app.services import planstore, regenerate, singleflight

ST = {"vacuum": True, "freeze": True, "shelf_life_days": 60, "note": ""}
PEPPER = [{"name": "перец болгарский", "qty": 200, "unit": "г", "category": "Овощи"},
          {"name": "говядина", "qty": 500, "unit": "г", "category": "Мясо и птица"}]
NO_PEPPER = [{"name": "говядина", "qty": 500, "unit": "г", "category": "Мясо и птица"}]


def _soup(ings=PEPPER) -> dict:
    return {"id": "soup", "name": "Суп", "emoji": "🍲", "servings": 4, "prep_min": 10,
            "cook_min": 40, "storage": ST, "ingredients": ings, "steps": ["варить"], "tips": [],
            "active_model": "anthropic",
            "variants": {"anthropic": {"ingredients": ings, "steps": ["варить"], "tips": [],
                                       "note": "", "provider": "Claude"}}}


def _other(i: str, name: str) -> dict:
    return {"id": i, "name": name, "emoji": "🥗", "servings": 4, "prep_min": 5, "cook_min": 5,
            "storage": ST}


def _detail(ings, steps) -> dict:
    return {"ingredients": ings, "steps": steps, "tips": [], "note": "", "provider": "Claude",
            "model": "anthropic", "model_ref": "anthropic:claude-haiku-4-5", "gen_id": "g1"}


def _soup_of(session, pid):
    session.expire_all()
    return next(d for d in session.get(PlanRow, pid).dishes if d["id"] == "soup")


def test_regenerate_in_old_version_moves_to_newer_versions(session, monkeypatch):
    """Устройство А добавило блюдо в чате (версия v2 из v1, потом v3), устройство Б с открытым
    рецептом v1 жмёт ↻ «без болгарского перца» — новый рецепт и в v2/v3, покупки идут без
    перца. В версии, где суп уже заменили, его не трогаем."""
    session.add(Conversation(id="c"))
    planstore.new_row(session, id="v1", conversation_id="c", title="П", week_label="w",
                      status="rejected", dishes=[_soup()])
    planstore.new_row(session, id="v2", conversation_id="c", title="П", week_label="w",
                      status="rejected", parent_id="v1", dishes=[_soup(), _other("x", "Салат")])
    planstore.new_row(session, id="v3", conversation_id="c", title="П", week_label="w",
                      status="accepted", parent_id="v2",
                      dishes=[_soup(), _other("x", "Салат"), _other("y", "Каша")])
    other_soup = {**_soup(), "name": "Суп-пюре", "variants": {}}
    planstore.new_row(session, id="v2b", conversation_id="c", title="П", week_label="w",
                      status="rejected", parent_id="v1", dishes=[other_soup])
    session.commit()

    async def fake_detail(name, servings=4, change="", model="", **kw):
        assert change == "без болгарского перца"
        return _detail(NO_PEPPER, ["варить без перца"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", fake_detail)
    row = session.get(PlanRow, "v1")
    asyncio.run(regenerate.regenerate_dish(session, row, "soup", "anthropic",
                                           change="без болгарского перца"))
    for pid in ("v1", "v2", "v3"):
        soup = _soup_of(session, pid)
        assert soup["steps"] == ["варить без перца"], pid
        assert soup["variants"]["anthropic"]["change"] == "без болгарского перца"
    assert _soup_of(session, "v2b") == other_soup  # заменённый суп — свой
    session.expire_all()
    names = [i["name"] for i in regenerate.shopping_base(session.get(PlanRow, "v3"))[0]]
    assert not any("перец" in n.lower() for n in names)


def test_join_waits_only_for_earlier_work():
    """Две работы, каждая из которых готова ждать другую, не виснут: ждут только начатую раньше."""
    order = []

    async def scenario():
        async def a():
            await asyncio.sleep(0)
            waited = await singleflight.join(lambda k: k == "B", own="A")
            order.append(("A", waited))

        async def b():
            await asyncio.sleep(0)
            waited = await singleflight.join(lambda k: k == "A", own="B")
            order.append(("B", waited))

        await asyncio.wait_for(asyncio.gather(
            singleflight.single_flight("A", a), singleflight.single_flight("B", b)
        ), timeout=2)

    asyncio.run(scenario())
    assert sorted(order) == [("A", False), ("B", True)]


def test_open_waits_for_running_backfill(session, monkeypatch):
    """Покупки догенерируют рецепт блюда, и тут же его открыли (другое устройство) — открытие
    ждёт догенерацию, а не зовёт модель второй раз."""
    session.add(Conversation(id="c"))
    planstore.new_row(session, id="p", conversation_id="c", title="П", week_label="w",
                      dishes=[_other("x", "Салат")])
    session.commit()
    calls = []

    async def slow_detail(name, servings=4, change="", model="", **kw):
        calls.append("backfill")
        await release.wait()
        return _detail(NO_PEPPER, ["из догенерации"])

    async def open_detail(*a, **kw):
        calls.append("open")
        return _detail(NO_PEPPER, ["открыли"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", slow_detail)
    monkeypatch.setattr(plans_router, "generate_dish_detail", open_detail)

    async def scenario():
        global release
        release = asyncio.Event()
        backfill = asyncio.create_task(regenerate.backfill_all(session, session.get(PlanRow, "p")))
        while "backfill" not in calls:
            await asyncio.sleep(0)
        opened = asyncio.create_task(plans_router.dish_details(
            "p", "x", DetailRequest(action="open"), session))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not opened.done()
        release.set()
        await backfill
        return await opened

    dish = asyncio.run(scenario())
    assert calls == ["backfill"] and dish.steps == ["из догенерации"]


def test_shopping_get_normalizes_once_for_parallel_requests(tmp_path, monkeypatch):
    """Покупки открыли на двух устройствах разом — одна нормализация, второй ждёт её."""
    from sqlalchemy import event
    from sqlmodel import SQLModel, create_engine

    eng = create_engine(f"sqlite:///{tmp_path / 'two.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _fast(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA synchronous=OFF")

    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(Conversation(id="c"))
        planstore.new_row(s, id="p", conversation_id="c", title="П", week_label="w",
                          dishes=[_soup()])
        s.commit()
    calls = []

    async def fake_normalize(base, discussion="", model=""):
        calls.append(len(base))
        await asyncio.sleep(0.05)
        return [{"name": "говядина", "qty": 500, "unit": "г", "category": "Мясо и птица"}]

    monkeypatch.setattr(plans_router, "normalize_shopping", fake_normalize)

    async def scenario():
        with Session(eng) as s1, Session(eng) as s2:
            return await asyncio.gather(plans_router.shopping_list("p", s1),
                                        plans_router.shopping_list("p", s2))

    g1, g2 = asyncio.run(scenario())
    assert len(calls) == 1
    assert [i.name for g in g1 for i in g.items] == [i.name for g in g2 for i in g.items] == ["говядина"]
