"""services/planstore: запись блюд плана точечно, с перечитыванием и compare-and-swap.

Гонки воспроизводим как в проде: отдельные сессии (= отдельные запросы) на файловой базе,
генерация модели «висит» на asyncio.Event, пока другой запрос пишет в тот же план."""

import ast
import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai import planner
from app.ai.base import AIError
from app.config import settings as config
from app.models import Conversation, MessageRow, PlanRow
from app.routers import chat as chat_router
from app.routers import plans as plans_router
from app.routers import recipes as recipes_router
from app.schemas import ChatRequest, DetailRequest, RecipeTextBody, RenameRequest, StatusRequest
from app.services import planstore, recipebook, regenerate, singleflight

ST = {"vacuum": True, "freeze": True, "shelf_life_days": 60, "note": ""}
ING = [{"name": "говядина", "qty": 500, "unit": "г", "category": "Мясо и птица"}]


def _detail(model: str, steps: list[str]) -> dict:
    return {"ingredients": ING, "steps": steps, "tips": [], "note": "", "provider": model.title(),
            "model": model, "model_ref": f"{model}:m", "gen_id": f"g-{model}-{steps[0]}"}


def _dishes() -> list[dict]:
    gulyash = {
        "id": "gulyash", "name": "Гуляш", "emoji": "🍲", "servings": 4, "prep_min": 10,
        "cook_min": 60, "storage": ST, "ingredients": ING, "steps": ["старый шаг"], "tips": [],
        "detail_provider": "DeepSeek", "active_model": "deepseek",
        "variants": {"deepseek": {"ingredients": ING, "steps": ["старый шаг"], "tips": [],
                                  "note": "", "provider": "DeepSeek"}},
    }
    borsch = {"id": "borsch", "name": "Борщ", "emoji": "🥣", "servings": 4, "prep_min": 5,
              "cook_min": 40, "storage": ST}
    return [gulyash, borsch]


@pytest.fixture()
def engine(tmp_path):
    """Файловая SQLite: у каждой сессии своё соединение, как у параллельных запросов."""
    eng = create_engine(f"sqlite:///{tmp_path / 'ps.db'}",
                        connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _no_fsync(dbapi_conn, _):  # тестовая база на SD-карте Пая: без fsync в разы быстрее
        dbapi_conn.execute("PRAGMA synchronous=OFF")

    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(Conversation(id="c1"))
        planstore.new_row(s, id="p1", conversation_id="c1", title="План", week_label="1–7",
                          dishes=_dishes())
        s.add(MessageRow(id="m0", conversation_id="c1", role="user", text="Мясное на неделю"))
        s.commit()
    yield eng
    eng.dispose()


def _dish(engine, pid: str, did: str) -> dict:
    with Session(engine) as s:
        return next(d for d in s.get(PlanRow, pid).dishes if d["id"] == did)


def _version(engine, pid: str) -> int:
    with Session(engine) as s:
        return s.get(PlanRow, pid).dishes_version


class _Key:
    key = "fake"


def test_backfill_during_regenerate_keeps_both(engine, monkeypatch):
    """Догенерация для покупок (Борщ) идёт, а в это время ↻ Гуляша успевает записаться.
    Раньше догенерация писала весь список из снимка до вызова модели — ↻ терялся."""
    release = {}

    async def fake_detail(name, servings=4, change="", model="", **kw):
        if name == "Борщ":
            await release["ev"].wait()
            return _detail("gemini", ["борщ"])
        return _detail("fake", ["новый гуляш"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", fake_detail)
    monkeypatch.setattr(regenerate, "gate_for", lambda m, task="chat": _Key)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            backfill = asyncio.create_task(regenerate.backfill_all(s1, s1.get(PlanRow, "p1")))
            for _ in range(3):  # догенерация взяла снимок и ждёт модель
                await asyncio.sleep(0)
            await regenerate.regenerate_dish(s2, s2.get(PlanRow, "p1"), "gulyash", "fake",
                                             change="поострее")
            release["ev"].set()
            await backfill

    asyncio.run(scenario())
    g, b = _dish(engine, "p1", "gulyash"), _dish(engine, "p1", "borsch")
    assert set(g["variants"]) == {"deepseek", "fake"} and g["active_model"] == "fake"
    assert g["steps"] == ["новый гуляш"]
    assert g["variants"]["fake"]["kind"] == "regenerate"
    assert g["variants"]["fake"]["change"] == "поострее"
    assert b["active_model"] == "gemini" and b["variants"]["gemini"]["kind"] == "backfill"
    assert _version(engine, "p1") == 2


def test_backfill_does_not_overwrite_recipe_opened_meanwhile(engine, monkeypatch):
    """Пока догенерация ждала модель, Борщ открыли (вариант Gemini) — догенерация его не
    перетирает: блюду рецепт уже не нужен."""
    release = {}

    async def slow_detail(name, servings=4, change="", model="", **kw):
        await release["ev"].wait()
        return _detail("deepseek", ["из догенерации"])

    async def open_detail(name, servings=4, change="", model="", **kw):
        return _detail("gemini", ["открыли"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", slow_detail)
    monkeypatch.setattr(plans_router, "generate_dish_detail", open_detail)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            row = s1.get(PlanRow, "p1")
            backfill = asyncio.create_task(regenerate.backfill_all(s1, row))
            for _ in range(3):
                await asyncio.sleep(0)
            await plans_router.dish_details(
                "p1", "borsch", DetailRequest(recipe_model="gemini", action="open"), s2
            )
            release["ev"].set()
            await backfill
            # row вызывающего (по ней дальше покупки/PDF/готовка) — с открытым рецептом, хотя
            # сама догенерация ничего не записала.
            b_row = next(d for d in row.dishes if d["id"] == "borsch")
            assert b_row["steps"] == ["открыли"] and b_row["ingredients"]
            base, _ = regenerate.shopping_base(row)
            assert [(i["name"], i["qty"], i["unit"]) for i in base] == [("Говядина", 1, "кг")]

    asyncio.run(scenario())
    b = _dish(engine, "p1", "borsch")
    assert set(b["variants"]) == {"gemini"} and b["steps"] == ["открыли"]
    assert _version(engine, "p1") == 1


async def _until(cond, limit: int = 50) -> None:
    """Крутим цикл, пока cond() не станет истинным (задачи дошли до ожидания модели)."""
    for _ in range(limit):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("задачи не дошли до ожидания модели")


BEET = [{"name": "свёкла", "qty": 300, "unit": "г", "category": "Овощи"}]


def test_shopping_page_requests_generate_recipe_once(engine, monkeypatch):
    """Прод 2026-10-09: страница покупок прислала /shopping-list и два /shopping-list/by-dish
    за ~6 с — каждый догенеривал рецепты сам (9 вызовов Haiku на 3 блюда, записался случайный).
    Теперь Борщ генерит один вызов модели, остальные запросы ждут его и строят ответ по
    записанному (свёкла есть во всех трёх ответах)."""
    events: list[asyncio.Event] = []

    async def gated_detail(name, servings=4, change="", model="", **kw):
        ev = asyncio.Event()
        events.append(ev)
        await ev.wait()
        return {**_detail("deepseek", ["борщ"]), "ingredients": BEET}

    async def same(items, discussion="", model=""):
        return items

    monkeypatch.setattr(regenerate, "generate_dish_detail", gated_detail)
    monkeypatch.setattr(plans_router, "normalize_shopping", same)

    async def scenario():
        with Session(engine) as s1, Session(engine) as s2, Session(engine) as s3:
            listing = asyncio.create_task(plans_router.shopping_list("p1", s1))
            await _until(lambda: len(events) == 1)
            by_dish = [asyncio.create_task(plans_router.shopping_by_dish("p1", s))
                       for s in (s2, s3)]
            for _ in range(20):  # оба «по рецептам» дошли до склейки и ждут генерацию списка
                await asyncio.sleep(0)
            assert len(events) == 1 and not any(t.done() for t in by_dish)
            events[0].set()
            return await asyncio.wait_for(asyncio.gather(listing, *by_dish), timeout=5)

    listing, *by = asyncio.run(scenario())
    assert len(events) == 1
    assert "Свёкла" in {it.name for g in listing for it in g.items}
    for res in by:
        assert "Свёкла" in {it.name for d in res for it in d.items}
    assert _version(engine, "p1") == 1
    assert not singleflight._inflight


def _seed_unbaked(engine) -> None:
    """План p3: у Борща и Плова рецепта нет, у Котлет только ингредиенты (без шагов — как
    спека пайплайна Cloudflare), у Гуляша рецепт есть."""
    gulyash, borsch = _dishes()
    plov = {"id": "plov", "name": "Плов", "emoji": "🍚", "servings": 4, "prep_min": 10,
            "cook_min": 60, "storage": ST}
    kotlety = {"id": "kotlety", "name": "Котлеты", "emoji": "🍖", "servings": 4, "prep_min": 10,
               "cook_min": 20, "storage": ST, "ingredients": ING}
    with Session(engine) as s:
        planstore.new_row(s, id="p3", conversation_id="c1", title="План", week_label="1–7",
                          dishes=[gulyash, borsch, plov, kotlety])
        s.commit()


def test_concurrent_backfills_generate_each_dish_once(engine, monkeypatch):
    """Два параллельных backfill_all — покупки (без шагов) и PDF/готовка (с шагами): каждое
    блюдо генерится ОДИН раз. Второй ждёт генерацию первого и читает записанное (деталь всегда
    с шагами — годится и готовке); Котлетам нужны только шаги — их генерит один второй."""
    _seed_unbaked(engine)
    calls: list[str] = []
    release = {}

    async def gated_detail(name, servings=4, change="", model="", **kw):
        calls.append(name)
        await release["ev"].wait()
        return _detail("deepseek", [f"шаг: {name}"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", gated_detail)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            r1, r2 = s1.get(PlanRow, "p3"), s2.get(PlanRow, "p3")
            shop = asyncio.create_task(regenerate.backfill_all(s1, r1))
            cook = asyncio.create_task(regenerate.backfill_all(s2, r2, need_steps=True))
            await _until(lambda: len(calls) == 3)
            for _ in range(20):  # больше генераций не начинается
                await asyncio.sleep(0)
            assert sorted(calls) == ["Борщ", "Котлеты", "Плов"]
            release["ev"].set()
            await asyncio.wait_for(asyncio.gather(shop, cook), timeout=5)
            # row обоих вызывающих (по ней дальше покупки/PDF/готовка) — со всеми рецептами,
            # хотя Борщ и Плов записал только первый, а Котлеты — только второй.
            for row in (r1, r2):
                assert all(d.get("steps") for d in row.dishes)

    asyncio.run(scenario())
    assert sorted(calls) == ["Борщ", "Котлеты", "Плов"]
    for did, name in (("borsch", "Борщ"), ("plov", "Плов"), ("kotlety", "Котлеты")):
        d = _dish(engine, "p3", did)
        assert d["steps"] == [f"шаг: {name}"] and d["variants"]["deepseek"]["kind"] == "backfill"
    assert _version(engine, "p3") == 3  # по записи на блюдо — никто не перезаписал соседа
    assert not singleflight._inflight


def test_backfill_with_other_model_waits_running_generation(engine, monkeypatch):
    """«Полный план» с выбранной моделью, пока покупки догенеривают Борщ моделью по умолчанию:
    ключ склейки — блюдо, без модели. Второй ждёт идущую генерацию, а не запускает свою —
    её рецепт fill всё равно бы не записал (блюдо уже с рецептом). Рецепт другой моделью —
    на странице блюда."""
    models: list[str] = []
    release = {}

    async def gated_detail(name, servings=4, change="", model="", **kw):
        models.append(model)
        await release["ev"].wait()
        return _detail("deepseek", ["борщ"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", gated_detail)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            shop = asyncio.create_task(regenerate.backfill_all(s1, s1.get(PlanRow, "p1")))
            await _until(lambda: len(models) == 1)
            full = asyncio.create_task(
                regenerate.backfill_all(s2, s2.get(PlanRow, "p1"), need_steps=True,
                                        model="gemini")
            )
            for _ in range(20):
                await asyncio.sleep(0)
            assert not full.done()
            release["ev"].set()
            return await asyncio.wait_for(asyncio.gather(shop, full), timeout=5)

    _, full = asyncio.run(scenario())
    assert models == [""]
    assert next(d for d in full if d["id"] == "borsch")["active_model"] == "deepseek"
    assert _version(engine, "p1") == 1


def test_backfill_failure_reaches_all_waiters_and_is_not_stuck(engine, monkeypatch):
    """Генерация упала: ошибку получают оба ждущих (блюдо без рецепта, наружу не летит, как и
    раньше), второго вызова модели нет; запись о задаче снята — следующий backfill_all
    пробует заново и записывает рецепт."""
    calls: list[str] = []
    release = {}

    async def failing(name, servings=4, change="", model="", **kw):
        calls.append(name)
        await release["ev"].wait()
        raise AIError("Anthropic: 529 overloaded")

    monkeypatch.setattr(regenerate, "generate_dish_detail", failing)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            first = asyncio.create_task(regenerate.backfill_all(s1, s1.get(PlanRow, "p1")))
            await _until(lambda: len(calls) == 1)
            second = asyncio.create_task(regenerate.backfill_all(s2, s2.get(PlanRow, "p1")))
            for _ in range(20):
                await asyncio.sleep(0)
            release["ev"].set()
            return await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    for dishes in asyncio.run(scenario()):
        assert not next(d for d in dishes if d["id"] == "borsch").get("ingredients")
    assert calls == ["Борщ"] and not singleflight._inflight
    assert _version(engine, "p1") == 0

    async def ok(name, servings=4, change="", model="", **kw):
        calls.append(name)
        return _detail("deepseek", ["борщ"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", ok)
    with Session(engine) as s:
        dishes = asyncio.run(regenerate.backfill_all(s, s.get(PlanRow, "p1")))
    assert calls == ["Борщ", "Борщ"]
    assert next(d for d in dishes if d["id"] == "borsch")["steps"] == ["борщ"]
    assert _version(engine, "p1") == 1


def test_cooking_backfill_sees_recipe_opened_meanwhile(engine, monkeypatch):
    """План готовки догенеривает Борщ, а его тем временем открыли: в модель готовки идут шаги
    открытого рецепта, а не блюдо без шагов (кэш готовки по составу держал бы это до ↻)."""
    release, seen = {}, []

    async def slow_detail(name, servings=4, change="", model="", **kw):
        await release["ev"].wait()
        return _detail("deepseek", ["из догенерации"])

    async def open_detail(name, servings=4, change="", model="", **kw):
        return _detail("gemini", ["открыли"])

    async def fake_cook(dishes, model="", **kw):
        seen.append({d["id"]: d.get("steps") for d in dishes})
        return {"steps": [], "note": "", "provider": "Fake"}

    monkeypatch.setattr(regenerate, "generate_dish_detail", slow_detail)
    monkeypatch.setattr(plans_router, "generate_dish_detail", open_detail)
    monkeypatch.setattr(plans_router, "generate_cooking_plan", fake_cook)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            cooking = asyncio.create_task(
                plans_router.cooking_plan("p1", DetailRequest(action="open"), s1)
            )
            for _ in range(6):  # готовка дошла до догенерации и ждёт модель
                await asyncio.sleep(0)
            await plans_router.dish_details(
                "p1", "borsch", DetailRequest(recipe_model="gemini", action="open"), s2
            )
            release["ev"].set()
            await cooking

    asyncio.run(scenario())
    assert seen == [{"gulyash": ["старый шаг"], "borsch": ["открыли"]}]


def test_open_keeps_regenerate_committed_meanwhile(engine, monkeypatch):
    """Первое открытие Борща (Gemini) ещё генерит, а ↻ Gemini с «Что учесть?» уже записался:
    открытие не перетирает явный ↻ пользователя — делает активным то, что есть."""
    release = {}

    async def slow_open(name, servings=4, change="", model="", **kw):
        await release["ev"].wait()
        return _detail("gemini", ["от открытия"])

    async def regen_detail(name, servings=4, change="", model="", **kw):
        return _detail("gemini", ["↻ без лука"])

    monkeypatch.setattr(plans_router, "generate_dish_detail", slow_open)
    monkeypatch.setattr(regenerate, "generate_dish_detail", regen_detail)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            opening = asyncio.create_task(plans_router.dish_details(
                "p1", "borsch", DetailRequest(recipe_model="gemini", action="open"), s1
            ))
            for _ in range(3):
                await asyncio.sleep(0)
            await plans_router.dish_details("p1", "borsch", DetailRequest(
                recipe_model="gemini", action="regenerate", note="без лука"), s2)
            release["ev"].set()
            return await opening

    res = asyncio.run(scenario())
    assert res.steps == ["↻ без лука"] and res.active_model == "gemini"
    v = _dish(engine, "p1", "borsch")["variants"]["gemini"]
    assert v["kind"] == "regenerate" and v["change"] == "без лука"
    assert _version(engine, "p1") == 1  # открытие ничего не записало


# --- одно и то же блюдо пишут двое: медленный пишет в СВЕЖЕЕ блюдо, а метаданные
# варианта (parent_id, ctx_uses) — по снимку, с которым ушёл в модель ---

LEEK = {"name": "порей", "qty": 1, "unit": "шт", "category": "Овощи"}


def _seed_leftovers(engine) -> None:
    """План p2 с остатком «порей»: оба блюда его пристраивают (uses). Сосед, у чьего рецепта
    порея нет, при записи меняет uses блюда (sync_uses) — снимок и свежее блюдо расходятся."""
    gulyash, borsch = _dishes()
    gulyash["ingredients"] = gulyash["variants"]["deepseek"]["ingredients"] = ING + [LEEK]
    for d in (gulyash, borsch):
        d["uses"] = ["порей"]
    with Session(engine) as s:
        planstore.new_row(s, id="p2", conversation_id="c1", title="План", week_label="1–7",
                          leftovers=["порей"], dishes=[gulyash, borsch])
        s.commit()


def test_open_while_backfill_same_dish_keeps_both(engine, monkeypatch):
    """Борщ открывают (Gemini, первая генерация ~20 с), а догенерация для покупок тем временем
    пишет ему DeepSeek: остаются оба варианта, активен открытый."""
    _seed_leftovers(engine)
    release = {}

    async def slow_open(name, servings=4, change="", model="", **kw):
        await release["ev"].wait()
        return _detail("gemini", ["открыли"])

    async def fast_backfill(name, servings=4, change="", model="", **kw):
        return _detail("deepseek", ["из догенерации"])

    monkeypatch.setattr(plans_router, "generate_dish_detail", slow_open)
    monkeypatch.setattr(regenerate, "generate_dish_detail", fast_backfill)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            opening = asyncio.create_task(plans_router.dish_details(
                "p2", "borsch", DetailRequest(recipe_model="gemini", action="open"), s1
            ))
            for _ in range(3):
                await asyncio.sleep(0)
            await regenerate.backfill_all(s2, s2.get(PlanRow, "p2"))
            assert _dish(engine, "p2", "borsch")["uses"] == []  # у DeepSeek порея нет
            release["ev"].set()
            await opening

    asyncio.run(scenario())
    b = _dish(engine, "p2", "borsch")
    assert set(b["variants"]) == {"gemini", "deepseek"} and b["active_model"] == "gemini"
    assert b["variants"]["deepseek"]["kind"] == "backfill"
    g = b["variants"]["gemini"]
    assert g["parent_id"] is None and g["ctx_uses"] == ["порей"]  # по снимку до модели


def test_regenerate_while_select_same_dish_keeps_both(engine, monkeypatch):
    """↻ Гуляша ждёт модель, а на другом устройстве выбрали Gemini: варианты всех трёх
    моделей целы, активен ↻; parent_id и ctx_uses ↻ — от блюда, по которому генерили."""
    _seed_leftovers(engine)
    release = {}

    async def slow_regen(name, servings=4, change="", model="", **kw):
        await release["ev"].wait()
        return _detail("fake", ["↻ гуляш"])

    async def fast_select(name, servings=4, change="", model="", **kw):
        return _detail("gemini", ["гуляш gemini"])

    monkeypatch.setattr(regenerate, "generate_dish_detail", slow_regen)
    monkeypatch.setattr(regenerate, "gate_for", lambda m, task="chat": _Key)
    monkeypatch.setattr(plans_router, "generate_dish_detail", fast_select)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            regen = asyncio.create_task(
                regenerate.regenerate_dish(s1, s1.get(PlanRow, "p2"), "gulyash", "fake")
            )
            for _ in range(3):
                await asyncio.sleep(0)
            await plans_router.dish_details(
                "p2", "gulyash", DetailRequest(recipe_model="gemini", action="select"), s2
            )
            g = _dish(engine, "p2", "gulyash")
            assert g["active_model"] == "gemini" and g["uses"] == []
            release["ev"].set()
            await regen

    asyncio.run(scenario())
    g = _dish(engine, "p2", "gulyash")
    assert set(g["variants"]) == {"deepseek", "gemini", "fake"} and g["active_model"] == "fake"
    v = g["variants"]["fake"]
    assert v["kind"] == "regenerate" and v["steps"] == ["↻ гуляш"]
    assert v["parent_id"] == "deepseek" and v["ctx_uses"] == ["порей"]


def test_chat_edit_keeps_recipe_committed_to_parent_meanwhile(engine, monkeypatch):
    """Правка в чате (добавить блюдо) ждёт модель, а в родителе в это время открыли Борщ.
    Новая версия берёт нетронутые правкой блюда из родителя НА МОМЕНТ записи — рецепт Борща
    не теряется (прод: 4d948d51 → 7f529202)."""
    release = {}

    async def fake_add(dishes, title, query, gender="f", model="", **kw):
        await release["ev"].wait()
        work = [dict(d) for d in dishes]
        work.append({"id": "dish-2-plov", "name": "Плов", "emoji": "🍚", "servings": 4,
                     "prep_min": 10, "cook_min": 50, "storage": ST})
        return {"reply": "Готово: добавлено «Плов».", "title": title, "dishes": work,
                "provider": "Fake", "changed": ["добавлено «Плов»"]}

    async def open_detail(name, servings=4, change="", model="", **kw):
        return _detail("gemini", ["борщ из родителя"])

    monkeypatch.setattr(chat_router, "add_dish_direct", fake_add)
    monkeypatch.setattr(plans_router, "generate_dish_detail", open_detail)
    monkeypatch.setattr(chat_router.chat_summary, "schedule", lambda *a, **kw: None)
    monkeypatch.setattr(chat_router.appstate, "get_current_plan", lambda: None)

    async def scenario():
        release["ev"] = asyncio.Event()
        with Session(engine) as s1, Session(engine) as s2:
            req = ChatRequest(conversation_id="c1", message="", recipe_model="deepseek",
                              add_dish=True)
            edit = asyncio.create_task(chat_router.chat_edit(req, s1))
            for _ in range(5):  # правка взяла снимок родителя и ждёт модель
                await asyncio.sleep(0)
            await plans_router.dish_details(
                "p1", "borsch", DetailRequest(recipe_model="gemini", action="open"), s2
            )
            release["ev"].set()
            return await edit

    res = asyncio.run(scenario())
    child = res.plan.id
    assert child != "p1" and [d.name for d in res.plan.dishes] == ["Гуляш", "Борщ", "Плов"]
    b = _dish(engine, child, "borsch")
    assert b["active_model"] == "gemini" and b["steps"] == ["борщ из родителя"]
    assert _dish(engine, child, "gulyash") == _dish(engine, "p1", "gulyash")
    with Session(engine) as s:
        parent = s.get(PlanRow, "p1")
        assert parent.status == "rejected" and s.get(PlanRow, child).parent_id == "p1"


def test_carry_current_takes_only_untouched_dishes():
    snap = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    result = [{"id": "a", "name": "A"}, {"id": "b", "name": "B", "steps": ["правка"]},
              {"id": "c", "name": "C"}]
    now = [{"id": "a", "name": "A", "steps": ["свежий"]}, {"id": "b", "name": "B", "steps": ["x"]}]
    out = planstore.carry_current(snap, result, now)
    assert out == [now[0], result[1], result[2]]


class SlowGate:
    """Рецепт приходит не сразу — оба запроса успевают дойти до записи вперемешку."""

    provider = "Fake"
    key = "fake"

    def __init__(self, parsed):
        self.parsed = parsed

    async def complete_json(self, messages, **kw):
        for _ in range(3):
            await asyncio.sleep(0)
        return dict(self.parsed), {}


RECIPE = {
    "name": "Сырники", "emoji": "🥞", "desc": "Творожные оладьи.", "servings": 4,
    "prep_min": 15, "cook_min": 20, "tags": ["завтрак"], "shelf_life_days": 60,
    "ingredients": [{"name": "творог", "qty": 500, "unit": "г", "category": "Молочное"}],
    "steps": ["смешай", "обжарь"], "tips": [], "note": "",
}


def test_two_concurrent_custom_recipes_both_saved(engine, monkeypatch):
    """Два своих рецепта собираются одновременно (сессии уже держат строку «Моих рецептов»).
    Раньше второй писал список из своей копии и затирал первый; теперь — оба, разные id."""
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": SlowGate(RECIPE))
    with Session(engine) as s:
        recipebook.library_row(s)

    async def scenario():
        with Session(engine) as s1, Session(engine) as s2:
            # строка уже в identity map каждой сессии (держим ссылки — карта слабая)
            held = [s.get(PlanRow, recipebook.LIBRARY_ID) for s in (s1, s2)]
            assert all(r.dishes == [] for r in held)
            return await asyncio.gather(
                recipes_router.create_custom_recipe(RecipeTextBody(text="сырники мамины"), s1),
                recipes_router.create_custom_recipe(RecipeTextBody(text="сырники бабушкины"), s2),
            )

    a, b = asyncio.run(scenario())
    with Session(engine) as s:
        lib = s.get(PlanRow, recipebook.LIBRARY_ID)
        dishes = lib.dishes
        assert lib.dishes_version == 2
    assert [d["id"] for d in dishes] == [a.dish_id, b.dish_id] and a.dish_id != b.dish_id
    assert {d["source"] for d in dishes} == {"сырники мамины", "сырники бабушкины"}
    assert all(d["variants"]["fake"]["kind"] == "custom" for d in dishes)


def test_cas_conflict_rereads_and_keeps_other_write(engine, monkeypatch):
    """Между нашим чтением и записью строку записал другой процесс: UPDATE с проверкой версии
    не проходит, перечитываем и пишем поверх свежего — запись соседа цела."""
    real_read = planstore.read_dishes
    calls = {"n": 0}

    def racing_read(session, plan_id):
        got = real_read(session, plan_id)
        calls["n"] += 1
        if calls["n"] == 1:
            with Session(engine) as other:
                planstore.patch_dishes(other, plan_id, {"borsch": lambda d: {**d, "emoji": "🍜"}})
        return got

    monkeypatch.setattr(planstore, "read_dishes", racing_read)
    with Session(engine) as s:
        out = planstore.patch_dishes(s, "p1", {"gulyash": lambda d: {**d, "emoji": "🥘"}})
    assert [d["emoji"] for d in out] == ["🥘", "🍜"]
    assert _dish(engine, "p1", "borsch")["emoji"] == "🍜"
    assert _dish(engine, "p1", "gulyash")["emoji"] == "🥘"
    assert _version(engine, "p1") == 2


def test_cas_gives_up_after_three_conflicts(engine, monkeypatch):
    real_read = planstore.read_dishes
    reads = []

    def stale_read(session, plan_id):
        dishes, version = real_read(session, plan_id)
        reads.append(version)
        return dishes, version - 1  # всегда «старая» версия — запись ни разу не пройдёт

    monkeypatch.setattr(planstore, "read_dishes", stale_read)
    with Session(engine) as s, pytest.raises(planstore.PlanConflict):
        planstore.patch_dishes(s, "p1", {"borsch": lambda d: {**d, "emoji": "🍜"}})
    assert len(reads) == 3
    assert _dish(engine, "p1", "borsch")["emoji"] == "🥣" and _version(engine, "p1") == 0


def test_patch_without_changes_does_not_write(engine):
    with Session(engine) as s:
        planstore.patch_dishes(s, "p1", {"borsch": lambda d: None, "gulyash": lambda d: d,
                                         "нет-такого": lambda d: {**d, "x": 1}})
    assert _version(engine, "p1") == 0
    with Session(engine) as s, pytest.raises(planstore.PlanNotFound):
        planstore.patch_dishes(s, "нет", {"a": lambda d: d})


def test_patch_cooking_merges_variants(engine):
    """Два плана готовки разными моделями: второй вливается в свежий кэш, первый цел;
    смена состава (другая подпись) начинает кэш заново."""
    v = lambda note: {"steps": [], "note": note, "provider": "X", "generated_at": ""}  # noqa: E731
    with Session(engine) as s:
        planstore.patch_cooking(s, "p1", "sig1", "deepseek", v("ds"))
        planstore.patch_cooking(s, "p1", "sig1", "gemini", v("gm"))
        cp = s.get(PlanRow, "p1").cooking_plan
        assert set(cp["variants"]) == {"deepseek", "gemini"} and cp["active_model"] == "gemini"
        planstore.patch_cooking(s, "p1", "sig1", "deepseek")  # только переключить
        assert s.get(PlanRow, "p1").cooking_plan["active_model"] == "deepseek"
        planstore.patch_cooking(s, "p1", "sig2", "gemini", v("new"))
        assert set(s.get(PlanRow, "p1").cooking_plan["variants"]) == {"gemini"}


# --- «Мои рецепты» (служебный план library) — не план недели ---


def test_library_guards(session):
    lib = recipebook.library_row(session)
    session.add(MessageRow(id="lm", conversation_id=lib.conversation_id, role="user",
                           text="Сырники без сахара?", discuss_target="recipe", dish_id="own-0"))
    session.commit()
    for call in (
        lambda: plans_router.delete_plan(lib.id, session),
        lambda: plans_router.rename_plan(lib.id, RenameRequest(title="Х"), session),
        lambda: plans_router.set_status(lib.id, StatusRequest(status="accepted"), session),
        lambda: chat_router.chat_edit(
            ChatRequest(conversation_id=lib.conversation_id, message="убери сырники"), session
        ),
    ):
        with pytest.raises(HTTPException) as e:
            asyncio.run(call())
        assert e.value.status_code == 409
    session.expire_all()
    row = session.get(PlanRow, recipebook.LIBRARY_ID)
    assert row.status == recipebook.LIBRARY_STATUS and row.title == "Мои рецепты"
    # правка в чате библиотеку не берёт; ссылки обсуждения своих рецептов — ведут в неё
    assert chat_router._latest_plan(session, lib.conversation_id) is None
    assert chat_router._latest_plan(session, lib.conversation_id, with_library=True).id == lib.id
    msgs = asyncio.run(chat_router.conversation_messages(lib.conversation_id, session))
    assert msgs[0].discuss_plan_id == recipebook.LIBRARY_ID


def test_library_conversation_draft_still_editable(session, monkeypatch):
    """Черновик плана, собранный в беседе «Моих рецептов» обычным сообщением, правится как
    любой план (✕ убирает блюдо); 409 — только когда править в беседе нечего."""
    lib = recipebook.library_row(session)
    planstore.new_row(session, id="draft", conversation_id=lib.conversation_id, title="План",
                      week_label="1–7", status="draft", dishes=_dishes())
    session.commit()
    monkeypatch.setattr(chat_router.appstate, "get_current_plan", lambda: None)
    res = asyncio.run(chat_router.chat_edit(
        ChatRequest(conversation_id=lib.conversation_id, message="", remove_dish_id="borsch"),
        session,
    ))
    assert [d.id for d in res.plan.dishes] == ["gulyash"]
    assert session.get(PlanRow, "draft").status == "rejected"
    lib_row = session.get(PlanRow, recipebook.LIBRARY_ID)
    assert lib_row.status == recipebook.LIBRARY_STATUS and lib_row.dishes == []


def test_plan_conflict_is_409_over_http(monkeypatch):
    """Сервисный слой без HTTP: PlanConflict → 409, PlanNotFound → 404 (обработчики в main)."""
    from app.db import engine as app_engine
    from app.db import init_db
    from app.main import app

    init_db()
    with Session(app_engine) as s:
        if s.get(Conversation, "c-409") is None:
            s.add(Conversation(id="c-409"))
        if s.get(PlanRow, "p-409") is None:
            planstore.new_row(s, id="p-409", conversation_id="c-409", title="П", week_label="w",
                              dishes=_dishes())
        s.commit()

    def boom(*a, **kw):
        raise planstore.PlanConflict("занято")

    monkeypatch.setattr(config, "app_password", "")
    monkeypatch.setattr(plans_router.planstore, "patch_dishes", boom)
    try:
        with TestClient(app) as c:
            r = c.post("/api/plans/p-409/dishes/gulyash/details", json={"action": "select",
                                                                        "recipeModel": "deepseek"})
            assert r.status_code == 409 and "повторите" in r.json()["detail"]

            def gone(*a, **kw):
                raise planstore.PlanNotFound("p-409")

            monkeypatch.setattr(plans_router.planstore, "patch_dishes", gone)
            r = c.post("/api/plans/p-409/dishes/gulyash/details", json={"action": "select",
                                                                        "recipeModel": "deepseek"})
            assert r.status_code == 404
    finally:
        with Session(app_engine) as s:
            row = s.get(PlanRow, "p-409")
            if row is not None:
                s.delete(row)
                s.commit()


# --- схема и единственный путь записи ---


def test_dishes_version_added_to_old_db(tmp_path, monkeypatch):
    """Старая база без dishes_version: _ensure_columns добавляет nullable-колонку, строки
    целы (NULL = версия 0)."""
    from app import db

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    SQLModel.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE planrow DROP COLUMN dishes_version"))
        conn.execute(text("INSERT INTO conversation (id, created_at) VALUES ('c', '2026-01-01')"))
        conn.execute(text(
            "INSERT INTO planrow (id, conversation_id, title, week_label, status, provider, "
            "dishes, shopping_cache, shopping_sig, shopping_model, cooking_plan, created_at) "
            "VALUES ('old', 'c', 't', 'w', 'draft', '', '[{\"id\": \"a\"}]', '[]', '', '', '{}', "
            "'2026-01-01')"
        ))
    monkeypatch.setattr(db, "engine", eng)
    db._ensure_columns()
    assert "dishes_version" in {c["name"] for c in inspect(eng).get_columns("planrow")}
    with Session(eng) as s:
        assert planstore.read_dishes(s, "old") == ([{"id": "a"}], 0)
        planstore.patch_dishes(s, "old", {"a": lambda d: {**d, "name": "A"}})
        assert s.exec(select(PlanRow.dishes_version).where(PlanRow.id == "old")).one() == 1
    eng.dispose()


# Пишут блюда плана / кэш готовки только эти модули (дальше — таблицы рецептов и миграции).
_WRITERS = {"services/planstore.py", "services/recipestore.py"}
_GUARDED = {"dishes", "cooking_plan"}
# Сырой SQL в обход ORM — та же запись мимо CAS.
_SQL_WRITE = re.compile(r"\bUPDATE\s+planrow\b.*\b(dishes|cooking_plan)\b", re.I | re.S)


def _dict_keys(args: list[ast.expr]) -> set[str]:
    """Ключи-константы словарей среди позиционных аргументов: .values({"dishes": …})."""
    return {
        k.value for a in args if isinstance(a, ast.Dict)
        for k in a.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _direct_writes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            for el in ast.walk(t):
                if isinstance(el, ast.Attribute) and el.attr in _GUARDED:
                    out.append(f"{path.name}:{node.lineno} .{el.attr} =")
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            hit = _GUARDED & {kw.arg for kw in node.keywords}
            # Словарь вместо keyword-аргументов: .values({...}), row.sqlmodel_update({...}),
            # PlanRow.model_validate({...}) (у схем ответа model_validate — не запись).
            on_planrow = isinstance(fn, ast.Attribute) and getattr(fn.value, "id", "") == "PlanRow"
            if name in ("values", "sqlmodel_update") or (name == "model_validate" and on_planrow):
                hit |= _GUARDED & _dict_keys(node.args)
            if name in ("PlanRow", "values", "update", "sqlmodel_update", "model_validate") and hit:
                out.append(f"{path.name}:{node.lineno} {name}({', '.join(sorted(hit))}=…)")
            if name in ("setattr", "flag_modified") and any(
                isinstance(a, ast.Constant) and a.value in _GUARDED for a in node.args
            ):
                out.append(f"{path.name}:{node.lineno} {name}(…)")
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and _SQL_WRITE.search(node.value)):
            out.append(f"{path.name}:{node.lineno} SQL UPDATE planrow")
    return out


def test_no_direct_plan_writes():
    """planrow.dishes / cooking_plan пишет только services/planstore (перечитывание + CAS):
    прямое присваивание снова открыло бы потерянные записи при параллельных запросах."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    bad: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(app_dir).as_posix()
        if rel in _WRITERS or rel.startswith("migrations/"):
            continue
        bad.extend(_direct_writes(path))
    assert bad == []


def test_guard_catches_direct_writes(tmp_path):
    sample = tmp_path / "bad.py"
    sample.write_text(
        "row.dishes = []\n"
        "row.cooking_plan = {}\n"
        "x = PlanRow(id='a', dishes=[])\n"
        "update(PlanRow).values(dishes=[])\n"
        "setattr(row, 'dishes', [])\n"
        "session.exec(update(PlanRow).values({'dishes': new}))\n"
        "session.exec(text('UPDATE planrow SET dishes = :d'), {'d': x})\n"
        "row.sqlmodel_update({'cooking_plan': {}})\n"
        "PlanRow.model_validate({'id': 'a', 'dishes': []})\n"
        # не запись: чтение, схема ответа, значения словаря, keyword чужой функции
        "dishes = list(row.dishes)\n"
        "f(dishes=dishes)\n"
        "WeekPlan.model_validate({'dishes': []})\n"
        "text('SELECT dishes FROM planrow')\n"
        "for v in variants.values(): pass\n",
        encoding="utf-8",
    )
    lines = sorted(int(hit.split(":")[1].split()[0]) for hit in _direct_writes(sample))
    assert lines == [1, 2, 3, 4, 5, 6, 7, 8, 9]
