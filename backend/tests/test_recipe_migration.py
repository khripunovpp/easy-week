"""Миграция recipes_v1 (app/migrations): все формы данных прода, сверка V1–V10,
идемпотентность, те же id после drop + apply, сбой посреди транзакции, правило ничьей
meta_hash, запись старым кодом → sync, защита живой базы в CLI.

Базы — файловые во временном каталоге (логический дамп до/после, CLI по пути)."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app import migrations
from app.migrations import __main__ as cli
from app.migrations import recipes_v1
from app.models import (
    RECIPE_METADATA,
    Conversation,
    FavoriteRecipe,
    MessageRow,
    PlanRow,
    RatingRow,
    Recipe,
    RecipeRevision,
)
from app.services import recipestore as rs
from app.services.variants import apply_variant

BACKEND = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 7, 1, 10, 0, 0)
ST = {"vacuum": True, "freeze": True, "shelf_life_days": 45}


def ing(name: str, qty: float = 500) -> list[dict]:
    return [{"name": name, "qty": qty, "unit": "г", "category": "Мясо и птица"}]


def var(provider: str, step: str, gen: str | None = "2026-07-01T10:00:00+00:00", **meta) -> dict:
    v = {"ingredients": ing(step), "steps": [step], "tips": [], "note": f"хранить {step}",
         "provider": provider}
    if gen is not None:
        v["generated_at"] = gen
    return {**v, **meta}


def vdish(did: str, name: str, variants: dict, active: str | None = None, **extra) -> dict:
    """Блюдо с вариантами + плоское зеркало активного (как пишет приложение)."""
    base = {"id": did, "name": name, "emoji": "🍲", "servings": 4, "prep_min": 10,
            "cook_min": 30, "tags": ["ужин"], "garnish": "", "storage": dict(ST), **extra}
    return apply_variant(base, active or next(iter(variants)), copy.deepcopy(variants))


def flat(did: str, name: str, provider: str, step: str, gen: str | None = None) -> dict:
    """Legacy-деталь: только плоские поля (июльские данные, старая догенерация)."""
    d = {"id": did, "name": name, "emoji": "🥘", "servings": 4, "prep_min": 5, "cook_min": 20,
         "tags": [], "storage": {**ST, "note": f"хранить {step}"}, "ingredients": ing(step),
         "steps": [step], "tips": ["совет"], "detail_provider": provider}
    if gen is not None:
        d["detail_generated_at"] = gen
    return d


def header(did: str, name: str) -> dict:
    return {"id": did, "name": name, "emoji": "🥗", "servings": 4, "prep_min": 5,
            "cook_min": 10, "tags": [], "storage": dict(ST), "ingredients": [], "steps": [],
            "tips": []}


def _engine(path: Path):
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _no_fsync(dbapi_conn, _):  # SD-карта Пая: без fsync тесты в разы быстрее
        dbapi_conn.execute("PRAGMA synchronous=OFF")

    return eng


def plan(s: Session, pid: str, conv: str, dishes: list[dict], *, day: int, status="draft",
         parent: str | None = None, decided: bool = False) -> None:
    if s.get(Conversation, conv) is None:
        s.add(Conversation(id=conv))
    s.add(PlanRow(id=pid, conversation_id=conv, title=f"План {pid}", week_label="1–7",
                  status=status, parent_id=parent, dishes=dishes,
                  created_at=T0 + timedelta(days=day),
                  decided_at=T0 + timedelta(days=day, hours=1) if decided else None))


A = var("DeepSeek", "борщ-A")
A2 = var("DeepSeek", "борщ-A2", "2026-07-02T10:00:00+00:00", model_ref="deepseek:deepseek-chat",
         kind="regenerate", change="погуще", parent_id="deepseek", ctx_uses=[], gen_id="g-a2")
K1 = var("DeepSeek", "котлеты-K1", None)  # без даты — оценочная (≈)
K2 = var("Claude", "котлеты-K2", "2026-07-01T11:00:00+00:00")
OWN_SRC = "Мои блины: мука, молоко\n\nУточнение: тоньше"
L = var("DeepSeek", "блины-L", "2026-07-08T10:00:00+00:00", kind="custom", model_ref="deepseek:x")


def build_prod_like(path: Path) -> None:
    """Все формы прода (design .final.migration шаг 12) в одной базе:
    c1 — цепочка версий p1→p2→p3: перезапись ↻ (A→A2), потерянный в гонке вариант (K2 есть
       только в p1), _reid с тем же id и другим названием (плов → рагу);
    c2 — тот же dish_id, что в c1, другой текст; legacy-детали по провайдерам (с датой и
       без), спека Cloudflare без провайдера;
    c3 — копия из Книги (котлеты c1) + два независимых корня одной беседы (кура в p5 и p6);
    c4 — висячий parent_id; library — свой рецепт с дописанным «Уточнение»; c5 — свой рецепт,
    взятый в план из Книги; оценки, избранное, реплики обсуждения."""
    eng = _engine(path)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        plan(s, "p1", "c1", [
            vdish("dish-0-борщ", "Борщ", {"deepseek": A}),
            header("dish-1-плов", "Плов"),
            vdish("dish-2-котлеты", "Котлеты", {"deepseek": K1, "anthropic": K2}),
        ], day=0, status="rejected")
        plan(s, "p2", "c1", [
            vdish("dish-0-борщ", "Борщ", {"deepseek": A2}),
            vdish("dish-1-плов", "Рагу", {"gemini": var("Gemini", "рагу-R")}),
            vdish("dish-2-котлеты", "Котлеты", {"deepseek": K1}),
        ], day=1, status="rejected", parent="p1")
        plan(s, "p3", "c1", [
            vdish("dish-0-борщ", "Борщ", {"deepseek": A2}),
            vdish("dish-1-плов", "Рагу", {"gemini": var("Gemini", "рагу-R")}),
            vdish("dish-2-котлеты", "Котлеты", {"deepseek": K1}),
        ], day=2, status="accepted", parent="p2", decided=True)
        plan(s, "p4", "c2", [
            flat("dish-0-борщ", "Борщ", "Claude", "борщ-B", "2026-07-05T09:00:00+00:00"),
            flat("dish-1-гуляш", "Гуляш", "DeepSeek", "гуляш"),
            flat("dish-2-рис", "Рис", "Cloudflare", "рис"),
            flat("dish-3-суп", "Суп", "Gemini", "суп"),
            {**header("dish-4-тефтели", "Тефтели"), "ingredients": ing("фарш"),
             "storage": {**ST, "note": "спека"}},
        ], day=4, status="accepted", decided=True)
        plan(s, "p5", "c3", [
            {**vdish("dish-0-котлеты", "Котлеты", {"deepseek": K1}), "from_book": True},
            vdish("dish-1-кура", "Кура", {"deepseek": var("DeepSeek", "кура-1")}),
        ], day=5)
        plan(s, "p6", "c3", [
            vdish("dish-1-кура", "Кура", {"anthropic": var("Claude", "кура-2")}),
        ], day=6)
        plan(s, "p7", "c4", [
            vdish("dish-0-щи", "Щи", {"deepseek": var("DeepSeek", "щи")}),
        ], day=6, parent="удалённый-план")
        plan(s, "library", "library", [
            vdish("own-0-блины", "Блины", {"deepseek": L}, source=OWN_SRC, uses=[]),
        ], day=7, status="library")
        plan(s, "p8", "c5", [
            {**vdish("dish-0-блины", "Блины", {"deepseek": L}, source=OWN_SRC),
             "from_book": True},
        ], day=8, status="accepted", decided=True)
        s.add_all([
            RatingRow(id="r-A", target_type="recipe", target_id="dish-0-борщ", model="deepseek",
                      vote=1, plan_id="p1", dish_id="dish-0-борщ"),
            RatingRow(id="r-K", target_type="recipe", target_id="dish-2-котлеты",
                      model="deepseek", vote=-1, plan_id="p3", dish_id="dish-2-котлеты"),
            RatingRow(id="r-gone", target_type="recipe", target_id="x", model="deepseek",
                      vote=1, plan_id="нет-плана", dish_id="x"),
            RatingRow(id="r-plan", target_type="plan", target_id="p3", model="deepseek", vote=1),
            FavoriteRecipe(key="борщ", name="Борщ", plan_id="p3", dish_id="dish-0-борщ"),
            FavoriteRecipe(key="блины", name="Блины", plan_id="library", dish_id="own-0-блины"),
            FavoriteRecipe(key="старое", name="Старое"),
            MessageRow(id="m1", conversation_id="c1", role="user", text="погуще?",
                       discuss_target="recipe", dish_id="dish-0-борщ"),
            MessageRow(id="m2", conversation_id="c1", role="assistant", text="да",
                       discuss_target="recipe", dish_id="dish-0-борщ"),
            MessageRow(id="m3", conversation_id="c1", role="user", text="?",
                       discuss_target="recipe", dish_id="dish-9-нет"),
            MessageRow(id="m4", conversation_id="c1", role="user", text="обычное"),
        ])
        s.commit()
    eng.dispose()


@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "easy_week.db"
    build_prod_like(p)
    return p


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path) -> list[str]:
    """Логическое содержимое базы (схема + данные). После отката транзакции оно прежнее, а
    байты — не обязательно: переиспользованные свободные страницы SQLite не журналирует."""
    import sqlite3

    c = sqlite3.connect(path)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return list(c.iterdump())
    finally:
        c.close()


def add_free_pages(path: Path) -> None:
    """Как на проде (81 свободная страница): таблица создана и снята — страницы в freelist."""
    import sqlite3

    c = sqlite3.connect(path)
    c.execute("CREATE TABLE junk (x TEXT)")
    c.executemany("INSERT INTO junk VALUES (?)", [("x" * 500,) for _ in range(400)])
    c.commit()
    c.execute("DROP TABLE junk")
    c.commit()
    assert c.execute("PRAGMA freelist_count").fetchone()[0] > 20
    c.close()


def q(path: Path, sql: str, **params):
    eng = _engine(path)
    try:
        with eng.connect() as c:
            return c.execute(text(sql), params).all()
    finally:
        eng.dispose()


def dishes_of(path: Path, pid: str) -> list[dict]:
    eng = _engine(path)
    try:
        with Session(eng) as s:
            return list(s.get(PlanRow, pid).dishes)
    finally:
        eng.dispose()


def rid_of(path: Path, pid: str, did: str) -> str:
    return next(d for d in dishes_of(path, pid) if d["id"] == did)["recipe_id"]


# --- перенос: числа, сверка, связи ---


def test_apply_counts_and_verify(db):
    res = cli.run_apply(db, backup=False)
    st = res["step"]["stats"]
    assert res["step"]["verify"]["ok"], res["step"]["verify"]["failed"]
    assert set(res["step"]["verify"]["checks"]) == {
        "V1_counts", "V2_additive", "V3_pins", "V4_parity", "V5_sigs", "V6_ratings",
        "V7_favorites", "V10_messages", "V8_book", "V9_integrity"}
    assert (st["recipes"], st["own"], st["revisions"]) == (11, 1, 13)
    assert (st["pinned"], st["plan_owned"], st["header_only"]) == (18, 1, 1)
    assert st["by_model"] == {"anthropic": 3, "cloudflare": 1, "deepseek": 7, "gemini": 2}
    assert st["estimated"] == 4  # K1 + три legacy-детали без detail_generated_at
    assert st["visible"] == 8 and st["book_copies_linked"] == 2
    assert st["dangling_parents"] == 1
    assert st["refs"] == {"ratings": 2, "overwritten": 0, "favorites": 2, "messages": 2,
                          "unresolved": 3,
                          "filled": {"ratings": 2, "favorites": 2, "messages": 2}}
    assert res["sync"]["revisions_new"] == 0 and res["sync"]["rows_changed"] == 0
    kinds = dict(q(db, "SELECT kind, count(*) FROM recipe_revision GROUP BY kind"))
    assert kinds == {"migrated": 11, "regenerate": 1, "custom": 1}
    marker = q(db, "SELECT name, app_commit FROM schema_migration")
    assert [m[0] for m in marker] == ["recipes_v1"]


def test_identity_rules(db):
    cli.run_apply(db, backup=False)
    borsch = rid_of(db, "p1", "dish-0-борщ")
    # Линия версий — один рецепт; перезаписанный ↻ текст A остался версией (только в p1).
    assert rid_of(db, "p2", "dish-0-борщ") == rid_of(db, "p3", "dish-0-борщ") == borsch
    revs = q(db, "SELECT steps, kind, change FROM recipe_revision WHERE recipe_id = :r "
                 "ORDER BY created_at", r=borsch)
    assert [(json.loads(r[0]), r[1], r[2]) for r in revs] == [
        (["борщ-A"], "migrated", ""), (["борщ-A2"], "regenerate", "погуще")]
    # Потерянный в гонке вариант Claude (только в p1) — версия того же рецепта котлет.
    kotl = rid_of(db, "p3", "dish-2-котлеты")
    assert rid_of(db, "p1", "dish-2-котлеты") == kotl
    assert {m for (m,) in q(db, "SELECT model FROM recipe_revision WHERE recipe_id = :r",
                            r=kotl)} == {"deepseek", "anthropic"}
    # _reid: тот же id блюда, другое название → другой рецепт (ключ линии — от p2).
    ragu = rid_of(db, "p2", "dish-1-плов")
    assert ragu == rs.recipe_id_for(rs.lineage_key("p2", "dish-1-плов", "рагу"))
    # Тот же dish_id в другой беседе — другой рецепт.
    assert rid_of(db, "p4", "dish-0-борщ") not in (borsch,)
    # Копия из Книги с тем же текстом — к источнику; два корня одной беседы — разные рецепты.
    assert rid_of(db, "p5", "dish-0-котлеты") == kotl
    assert rid_of(db, "p5", "dish-1-кура") != rid_of(db, "p6", "dish-1-кура")
    # Свой рецепт: ключ own:, source — текст как есть (с «Уточнение»); копия в плане — к нему.
    own = rid_of(db, "library", "own-0-блины")
    assert own == rs.recipe_id_for("own:own-0-блины") == rid_of(db, "p8", "dish-0-блины")
    assert q(db, "SELECT origin, source FROM recipe WHERE id = :r", r=own) == [("own", OWN_SRC)]
    # Висячий родитель — сам себе корень.
    assert rid_of(db, "p7", "dish-0-щи") == rs.recipe_id_for(
        rs.lineage_key("p7", "dish-0-щи", "щи"))
    # Спека без провайдера и блюдо-шапка — без закреплений (остаются в JSON как есть).
    p4 = {d["id"]: d for d in dishes_of(db, "p4")}
    assert "recipe_id" not in p4["dish-4-тефтели"] and p4["dish-4-тефтели"]["ingredients"]
    assert "recipe_id" not in dishes_of(db, "p1")[1]


def test_legacy_flat_keeps_date_and_provider(db):
    cli.run_apply(db, backup=False)
    p4 = {d["id"]: d for d in dishes_of(db, "p4")}
    for did, model in (("dish-0-борщ", "anthropic"), ("dish-1-гуляш", "deepseek"),
                       ("dish-2-рис", "cloudflare"), ("dish-3-суп", "gemini")):
        assert list(p4[did]["rev_ids"]) == [model]
        assert "active_model" not in p4[did] and "variants" not in p4[did]  # JSON не тронут
    rows = q(db, "SELECT generated_at FROM recipe_revision WHERE model = 'anthropic' "
                 "AND steps = :s", s=json.dumps(["борщ-B"]))
    assert rows == [("2026-07-05T09:00:00+00:00",)]
    est = q(db, "SELECT created_at_estimated, created_at FROM recipe_revision "
                "WHERE steps = :s", s=json.dumps(["гуляш"]))
    assert est[0][0] == 1 and est[0][1].startswith("2026-07-05")  # дата версии плана


def test_refs_map_exact_voted_content(db):
    cli.run_apply(db, backup=False)
    borsch = rid_of(db, "p1", "dish-0-борщ")
    # Голос в p1 — за текст A (не за перезаписавший его A2 из p3).
    (rec, rev), = q(db, "SELECT recipe_id, revision_id FROM ratingrow WHERE id = 'r-A'")
    assert rec == borsch
    assert q(db, "SELECT steps FROM recipe_revision WHERE id = :r", r=rev) == [
        (json.dumps(["борщ-A"]),)]
    assert q(db, "SELECT recipe_id, revision_id FROM ratingrow WHERE id IN ('r-gone', 'r-plan')"
             ) == [(None, None), (None, None)]
    favs = dict(q(db, "SELECT key, recipe_id FROM favoriterecipe"))
    assert favs == {"борщ": borsch, "блины": rid_of(db, "library", "own-0-блины"),
                    "старое": None}
    msgs = dict(q(db, "SELECT id, recipe_id FROM messagerow"))
    assert msgs == {"m1": borsch, "m2": borsch, "m3": None, "m4": None}


def test_vote_before_in_place_regenerate_keeps_only_recipe(tmp_path):
    """👎 за текст, который потом «↻» перезаписал в той же версии плана: оценённого текста
    больше нет нигде — оценка получает только рецепт (revision_id NULL), а не версию,
    сгенерированную после голоса. Голос после ↻ и голос за текст без даты — к закреплению."""
    p = tmp_path / "vote.db"
    eng = _engine(p)
    SQLModel.metadata.create_all(eng)
    t2 = "2026-07-01T12:53:54+00:00"  # ↻ — после первого голоса (12:47)
    with Session(eng) as s:
        plan(s, "v1", "c", [
            vdish("dish-0-котлеты", "Котлеты", {"anthropic": var("Claude", "без оливок", t2)}),
            vdish("dish-1-суп", "Суп", {"deepseek": var("DeepSeek", "суп", None)}),
        ], day=0)
        s.add_all([
            RatingRow(id="r-before", target_type="recipe", target_id="dish-0-котлеты",
                      model="anthropic", vote=-1, plan_id="v1", dish_id="dish-0-котлеты",
                      created_at=datetime(2026, 7, 1, 12, 47, 3)),
            RatingRow(id="r-after", target_type="recipe", target_id="x", model="anthropic",
                      vote=1, plan_id="v1", dish_id="dish-0-котлеты",
                      created_at=datetime(2026, 7, 1, 13, 0, 0)),
            # текст без даты (≈ дата версии плана) — сравнить не с чем, верим закреплению
            RatingRow(id="r-est", target_type="recipe", target_id="dish-1-суп",
                      model="deepseek", vote=1, plan_id="v1", dish_id="dish-1-суп",
                      created_at=datetime(2026, 6, 1)),
        ])
        s.commit()
    eng.dispose()
    res = cli.run_apply(p, backup=False)
    assert res["step"]["verify"]["ok"], res["step"]["verify"]["failed"]
    assert res["step"]["stats"]["refs"]["overwritten"] == 1
    kotl = rid_of(p, "v1", "dish-0-котлеты")
    pins = {d["id"]: d["rev_ids"] for d in dishes_of(p, "v1")}
    refs = {r[0]: tuple(r[1:]) for r in q(p, "SELECT id, recipe_id, revision_id FROM ratingrow")}
    assert refs["r-before"] == (kotl, None)
    assert refs["r-after"] == (kotl, pins["dish-0-котлеты"]["anthropic"])
    assert refs["r-est"] == (rid_of(p, "v1", "dish-1-суп"), pins["dish-1-суп"]["deepseek"])
    # повторный apply (sync) ничего не дописывает: NULL в revision_id — честное «неизвестно»
    again = cli.run_apply(p, backup=False)
    assert again["step"] is None and sum(again["sync"]["refs_filled"].values()) == 0
    assert q(p, "SELECT recipe_id, revision_id FROM ratingrow WHERE id = 'r-before'") == [
        (kotl, None)]
    strict = cli.run_readonly(p, lambda s: recipes_v1.verify_session(s, strict_refs=True))
    assert strict["ok"], strict["failed"]


def test_accepted_plan_owned_dish_with_body_stays_in_book(db):
    """Блюдо принятого плана с шагами, но без провайдера и вариантов (модель плана вернула
    шаги — planner._clean_dish их не режет) — своё в плане, без закреплений. Оно в Книге и по
    JSON, и по таблицам: V8 не падает, apply/sync/verify проходят."""
    eng = _engine(db)
    with Session(eng) as s:
        plan(s, "p-own", "c6", [{**header("dish-0-шарлотка", "Шарлотка"),
                                 "ingredients": ing("яблоки"), "steps": ["Испеки"]}],
             day=9, status="accepted", decided=True)
        s.commit()
    eng.dispose()
    res = cli.run_apply(db, backup=False)
    assert res["step"]["verify"]["ok"], res["step"]["verify"]["failed"]
    assert res["step"]["stats"]["plan_owned"] == 2
    assert "recipe_id" not in dishes_of(db, "p-own")[0]

    def names(s: Session):
        from app.services.recipebook import book_names

        return (book_names(s), book_names(s, index=rs.book_index_tables(s)),
                recipes_v1.verify_session(s, strict_refs=False)["ok"])

    by_json, by_tables, ok = cli.run_readonly(db, names)
    assert "Шарлотка" in by_json and by_json == by_tables and ok
    assert cli._tx(db, recipes_v1.sync_session, name="recipes_sync")["verify"]["ok"]


def test_sync_warns_only_about_empty_refs(db, caplog):
    """sync не пересчитывает и не «теряет» уже записанные ссылки: реплики, чьё блюдо потом
    убрали из свежей версии плана, остаются со своим рецептом и без ложного WARNING."""
    import logging

    cli.run_apply(db, backup=False)
    borsch = rid_of(db, "p3", "dish-0-борщ")
    _raw_update(db, "p3", [d for d in dishes_of(db, "p3") if d["id"] != "dish-0-борщ"])
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="easy_week.migrations")
    res = cli._tx(db, recipes_v1.sync_session, name="recipes_sync")
    assert res["verify"]["ok"]
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "m1" not in warned and "m2" not in warned
    assert "messagerow:m3" in warned and "ratingrow:r-gone" in warned  # они и правда пустые
    assert dict(q(db, "SELECT id, recipe_id FROM messagerow WHERE id IN ('m1', 'm2')")) == {
        "m1": borsch, "m2": borsch}


def test_json_only_gains_pins(db):
    before = {pid: dishes_of(db, pid) for pid in ("p1", "p3", "p4", "library")}
    raw_before = dict(q(db, "SELECT id, dishes FROM planrow"))
    cli.run_apply(db, backup=False)
    for pid, old in before.items():
        new = dishes_of(db, pid)
        assert [rs.strip_pins(d) for d in new] == old
        for d in new:
            if "rev_ids" in d:
                # ключи добавлены в конец, active_model и остальное — на месте
                assert list(d)[-2:] == ["recipe_id", "rev_ids"]
    raw_after = dict(q(db, "SELECT id, dishes FROM planrow"))
    # строки без рецептов не переписаны вовсе (байт-в-байт)
    assert all(raw_after[k] == v for k, v in raw_before.items()
               if not any(rs.variants_for(d) for d in dishes_of(db, k)))


# --- идемпотентность, drop + apply, strip ---


def _pins(path: Path):
    return cli._snapshot(path)


def test_idempotent_and_same_ids_after_drop(db):
    cli.run_apply(db, backup=False)
    pins1, ids1 = _pins(db)
    raw1 = dict(q(db, "SELECT id, dishes FROM planrow"))
    again = cli.run_apply(db, backup=False)
    assert again["step"] is None
    assert {k: again["sync"][k] for k in ("revisions_new", "recipes_new", "rows_changed",
                                          "headers_refreshed")} == dict.fromkeys(
        ("revisions_new", "recipes_new", "rows_changed", "headers_refreshed"), 0)
    assert sum(again["sync"]["refs_filled"].values()) == 0
    assert dict(q(db, "SELECT id, dishes FROM planrow")) == raw1

    # drop без strip: закрепления в JSON → те же строки с теми же id
    cli._tx(db, recipes_v1.drop_session, name="drop")
    assert not inspect(_engine(db)).has_table("recipe")
    assert q(db, "SELECT count(*) FROM ratingrow WHERE recipe_id IS NOT NULL") == [(0,)]
    cli.run_apply(db, backup=False)
    assert _pins(db) == (pins1, ids1)

    # strip + drop + apply: всё заново из JSON — те же id
    cli._tx(db, recipes_v1.strip_session, name="strip")
    assert not any("rev_ids" in d for pid in ("p1", "p3") for d in dishes_of(db, pid))
    cli._tx(db, recipes_v1.drop_session, name="drop")
    cli.run_apply(db, backup=False)
    assert _pins(db) == (pins1, ids1)


def test_rehearse_on_copy_leaves_source_untouched(db, tmp_path):
    before = sha(db)
    rep = cli.run_rehearse(db, tmp_path / "work")
    assert rep["ok"] and all(c["ok"] for c in rep["checks"].values())
    assert rep["checks"]["strip_drop_apply_same_ids"]["same"]
    assert sha(db) == before and list((tmp_path / "work").iterdir()) == []
    assert not inspect(_engine(db)).has_table("recipe")


def test_rehearse_work_dir_removes_only_its_own_copy(db, tmp_path):
    """--work задаёт человек: каталог с чужими файлами или каталог самой базы. Удаляется только
    свой подкаталог репетиции; копия «в себя» (work = каталог базы) невозможна."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("чужой файл", encoding="utf-8")
    assert cli.run_rehearse(db, work)["ok"]
    assert [p.name for p in work.iterdir()] == ["notes.txt"]

    before = sha(db)
    (db.parent / "keep.txt").write_text("рядом с базой", encoding="utf-8")
    assert cli.main(["rehearse", "--db", str(db), "--work", str(db.parent)]) == 0
    assert sha(db) == before and (db.parent / "keep.txt").exists()
    assert not list(db.parent.glob("rehearse-*"))
    # --keep: копия остаётся в своём подкаталоге, а не поверх источника
    rep = cli.run_rehearse(db, db.parent, keep=True)
    kept = Path(rep["copy"])
    assert kept.parent.parent == db.parent and kept.parent.name.startswith("rehearse-")
    assert kept.exists() and sha(db) == before


# --- сбой посреди транзакции ---


@pytest.mark.parametrize("free_pages", [False, True])
@pytest.mark.parametrize("stage", ["after_write", "after_verify"])
def test_crash_mid_apply_leaves_db_logically_identical(db, stage, free_pages):
    """Сбой посреди транзакции — данные и схема прежние. Байты — только если свободных страниц
    нет: на проде они есть, и SQLite не журналирует переиспользованные (дамп совпадает)."""
    if free_pages:
        add_free_pages(db)
    before, before_sha = dump(db), sha(db)

    def boom(st):
        if st == stage:
            raise RuntimeError("сбой посреди миграции")

    with pytest.raises(RuntimeError):
        cli.run_apply(db, backup=False, hook=boom)
    assert dump(db) == before
    if not free_pages:
        assert sha(db) == before_sha
    assert not Path(f"{db}-journal").exists()
    assert not inspect(_engine(db)).has_table("recipe")
    # следующий деплой просто повторяет — и проходит
    assert cli.run_apply(db, backup=False)["step"]["verify"]["ok"]


def _kill_mid_apply(db: Path) -> None:
    """Процесс миграции падает (os._exit) после записи, до COMMIT. Кэш страниц — крошечный:
    SQLite выталкивает изменённые страницы в файл базы ещё до COMMIT (как на большой базе), и
    остаётся настоящий горячий журнал (на маленькой базе всё сидело бы в кэше)."""
    code = (
        "import os, sys; from pathlib import Path\n"
        "from sqlalchemy import event\n"
        "from app.migrations import __main__ as cli\n"
        "real = cli.open_engine\n"
        "def small_cache(path, readonly=False):\n"
        "    eng = real(path, readonly=readonly)\n"
        "    event.listen(eng, 'connect', lambda c, _: c.execute('PRAGMA cache_size=5'))\n"
        "    return eng\n"
        "cli.open_engine = small_cache\n"
        "cli.run_apply(Path(sys.argv[1]), backup=False,"
        " hook=lambda st: os._exit(7) if st == 'after_write' else None)\n"
    )
    env = {**os.environ, "DB_PATH": str(db.parent / "unused.db")}
    proc = subprocess.run([sys.executable, "-c", code, str(db)], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 7, proc.stderr[-500:]
    assert Path(f"{db}-journal").read_bytes()[:8] != bytes(8)  # заголовок есть — журнал горячий


def test_hot_journal_readers_fail_cleanly(db, capsys):
    """Горячий журнал после убитого apply: чтение (mode=ro) не может его откатить — status,
    verify и rehearse выходят с понятной ошибкой (код 1), а не трассировкой."""
    _kill_mid_apply(db)
    with pytest.raises(migrations.MigrationError, match="горячий журнал"):
        migrations.copy_db(db, db.parent / "copy" / "x.db")
    assert cli.main(["status", "--db", str(db)]) == 1
    assert cli.main(["rehearse", "--db", str(db)]) == 1
    assert "горячий журнал" in capsys.readouterr().err
    assert not list((db.parent / "backups").glob("rehearse-*"))  # пустой каталог убран


def test_process_killed_mid_apply_recovers_logically_identical(db):
    """Жёсткое падение процесса (os._exit) посреди транзакции: SQLite откатывает горячий
    журнал при следующем открытии — данные как до миграции."""
    add_free_pages(db)
    before = dump(db)
    _kill_mid_apply(db)
    eng = _engine(db)
    with eng.connect() as c:  # первое открытие откатывает горячий журнал
        assert c.execute(text("SELECT count(*) FROM planrow")).scalar() == 9
    eng.dispose()
    assert dump(db) == before


@pytest.mark.parametrize("sql", [
    "DELETE FROM messagerow WHERE id = 'm4'",
    "UPDATE planrow SET title = 'другое' WHERE id = 'p4'",
    "UPDATE ratingrow SET vote = -vote WHERE id = 'r-plan'",
    "DELETE FROM favoriterecipe WHERE key = 'старое'",
    "UPDATE conversation SET summary = 'x' WHERE id = 'c1'",
    "INSERT INTO messagerow (id, conversation_id, role, text, model, created_at) "
    "VALUES ('m-new', 'c1', 'user', 'x', '', '2026-07-01')",
])
@pytest.mark.parametrize("migrated", [False, True])
def test_v2_catches_lost_or_changed_rows(db, monkeypatch, sql, migrated):
    """Строки бесед/планов/сообщений/оценок/избранного миграция и sync не удаляют и не меняют
    (кроме ссылок на рецепт): такая запись в их транзакции → V2, откат."""
    if migrated:
        cli.run_apply(db, backup=False)
    before = dump(db)
    real = recipes_v1.backfill_refs

    def sneaky(session, rows, b):
        session.exec(text(sql))
        return real(session, rows, b)

    monkeypatch.setattr(recipes_v1, "backfill_refs", sneaky)
    with pytest.raises(cli.SyncFailed if migrated else recipes_v1.VerifyFailed) as exc:
        cli.run_apply(db, backup=False)
    failed = exc.value.__cause__ if migrated else exc.value
    assert failed.report["failed"] == ["V2_additive"]
    assert dump(db) == before


def test_verify_failure_rolls_back_and_writes_report(db, monkeypatch):
    before = sha(db)
    real = recipes_v1.verify_session

    def broken(session, **kw):
        rep = real(session, **kw)
        rep["checks"]["V4_parity"]["ok"] = False
        return {**rep, "ok": False, "failed": ["V4_parity"]}

    monkeypatch.setattr(recipes_v1, "verify_session", broken)
    with pytest.raises(recipes_v1.VerifyFailed):
        cli.run_apply(db, backup=False)
    assert sha(db) == before
    reports = list((db.parent / "backups").glob("recipes_v1-verify-*.json"))
    assert len(reports) == 1 and "V4_parity" in reports[0].read_text(encoding="utf-8")


# --- правило ничьей и запись старым кодом ---


def test_meta_hash_tie_rule(tmp_path):
    """Тот же текст с другой датой/провайдером/source — отдельные версии (не «победитель»)."""
    p = tmp_path / "tie.db"
    eng = _engine(p)
    SQLModel.metadata.create_all(eng)
    same = var("DeepSeek", "один-текст", "2026-07-01T10:00:00+00:00")
    with Session(eng) as s:
        plan(s, "t1", "c", [vdish("dish-0-x", "Икс", {"deepseek": same})], day=0)
        plan(s, "t2", "c", [vdish("dish-0-x", "Икс", {
            "deepseek": {**same, "generated_at": "2026-07-02T10:00:00+00:00"}})],
            day=1, parent="t1")
        plan(s, "t3", "c", [vdish("dish-0-x", "Икс", {"deepseek": {**same, "provider": "Другой"}})],
             day=2, parent="t2")
        plan(s, "t4", "c", [vdish("dish-0-x", "Икс", {"deepseek": same}, source="текст")],
             day=3, parent="t3")
        s.commit()
    eng.dispose()
    res = cli.run_apply(p, backup=False)
    assert res["step"]["verify"]["ok"]
    rows = q(p, "SELECT content_hash, meta_hash FROM recipe_revision")
    assert len(rows) == 4 and len({h for h, _ in rows}) == 1 and len({m for _, m in rows}) == 4
    assert len({rid_of(p, t, "dish-0-x") for t in ("t1", "t2", "t3", "t4")}) == 1


def _raw_update(path: Path, pid: str, dishes: list[dict]) -> None:
    """Запись старым кодом: мимо planstore и двойной записи (JSON — как пишет SQLAlchemy)."""
    eng = _engine(path)
    with Session(eng) as s:
        s.get(PlanRow, pid).dishes = dishes  # noqa: guard — тест имитирует старый код
        s.commit()
    eng.dispose()


def test_old_code_writes_then_sync_resyncs(db):
    cli.run_apply(db, backup=False)
    revs_before = {r for (r,) in q(db, "SELECT id FROM recipe_revision")}
    # 1) старый ↻: перезаписал вариант deepseek борща в p3 (без метаданных), rev_ids устарели
    p3 = dishes_of(db, "p3")
    old_pin = p3[0]["rev_ids"]["deepseek"]
    p3[0] = apply_variant(p3[0], "deepseek", {"deepseek": var("DeepSeek", "борщ-A3",
                                                              "2026-07-20T10:00:00+00:00")})
    _raw_update(db, "p3", p3)
    # 2) старая правка в чате: новая версия p9 от p3 — копии блюд с закреплениями + котлеты
    # заменены правкой (новый текст, старые закрепления), без двойной записи
    eng = _engine(db)
    with Session(eng) as s:
        kids = copy.deepcopy(dishes_of(db, "p3"))
        kids[2] = apply_variant(kids[2], "deepseek", {"deepseek": var("DeepSeek", "котлеты-K9")})
        plan(s, "p9", "c1", kids, day=20, parent="p3")
        s.commit()
    eng.dispose()

    # верификация видит расхождение (закрепление ≠ текст), sync его лечит
    ver = cli.run_readonly(db, lambda s: recipes_v1.verify_session(s, strict_refs=False))
    assert not ver["ok"] and "V3_pins" in ver["failed"]
    res = cli._tx(db, recipes_v1.sync_session, name="recipes_sync")
    assert res["revisions_new"] == 2 and res["kinds_new"] == {"resync": 2}
    # перезакреплены: борщ в p3 и в p9 (устаревшие закрепления), котлеты в p9
    assert res["repinned"] == 3 and res["verify"]["ok"]
    new = q(db, "SELECT steps, kind, parent_id FROM recipe_revision WHERE kind = 'resync' "
                "ORDER BY steps")
    assert [json.loads(n[0]) for n in new] == [["борщ-A3"], ["котлеты-K9"]]
    assert new[0][2] == old_pin  # от какой версии шли — прежнее закрепление этой модели
    # ничего не потеряно: прежние версии на месте, новый план закреплён за тем же рецептом
    assert revs_before <= {r for (r,) in q(db, "SELECT id FROM recipe_revision")}
    assert rid_of(db, "p9", "dish-2-котлеты") == rid_of(db, "p1", "dish-2-котлеты")
    again = cli._tx(db, recipes_v1.sync_session, name="recipes_sync")
    assert again["revisions_new"] == 0 and again["rows_changed"] == 0


def test_root_deleted_after_pinning_then_sync(db):
    cli.run_apply(db, backup=False)
    borsch = rid_of(db, "p3", "dish-0-борщ")
    eng = _engine(db)
    with Session(eng) as s:
        s.delete(s.get(PlanRow, "p1"))
        s.commit()
    eng.dispose()
    res = cli._tx(db, recipes_v1.sync_session, name="recipes_sync")
    assert res["verify"]["ok"] and res["rows_changed"] == 0 and res["recipes_new"] == 0
    assert rid_of(db, "p2", "dish-0-борщ") == rid_of(db, "p3", "dish-0-борщ") == borsch
    # версия текста, жившего только в удалённом p1, остаётся в истории
    assert q(db, "SELECT count(*) FROM recipe_revision WHERE recipe_id = :r", r=borsch) == [(2,)]


# --- схема и защита живой базы ---


def _schema(path: Path) -> dict:
    out = {}
    for t in ("recipe", "recipe_revision", "schema_migration", "ratingrow", "favoriterecipe",
              "messagerow"):
        out[t] = [(r[1], r[2], r[3]) for r in q(path, f'PRAGMA table_info("{t}")')]
    out["idx"] = sorted(r[0] for r in q(
        path, "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger') "
              "AND tbl_name IN ('recipe', 'recipe_revision', 'schema_migration')"))
    return out


def test_schema_same_on_fresh_and_evolved_db(tmp_path):
    """Схема после миграции одна и та же: свежая база и база эпохи до фазы 0b
    (без dishes_version) после init_db-подобного _ensure_columns + шага."""
    from app import db as app_db

    fresh = tmp_path / "fresh.db"
    eng = _engine(fresh)
    SQLModel.metadata.create_all(eng)
    eng.dispose()
    cli.run_apply(fresh, backup=False)

    old = tmp_path / "old.db"
    eng = _engine(old)
    SQLModel.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE planrow DROP COLUMN dishes_version"))
    saved = app_db.engine
    app_db.engine = eng
    try:
        app_db._ensure_columns()
    finally:
        app_db.engine = saved
    eng.dispose()
    cli.run_apply(old, backup=False)
    assert _schema(fresh) == _schema(old)
    assert ("recipe_id", "VARCHAR", 0) in _schema(fresh)["ratingrow"]


def test_app_startup_never_creates_recipe_schema(tmp_path, monkeypatch):
    """init_db (старт приложения) не создаёт таблицы рецептов и колонки ссылок."""
    from app import db as app_db

    eng = _engine(tmp_path / "start.db")
    monkeypatch.setattr(app_db, "engine", eng)
    app_db.init_db()
    insp = inspect(eng)
    assert not set(RECIPE_METADATA.tables) & set(insp.get_table_names())
    assert "recipe_id" not in {c["name"] for c in insp.get_columns("ratingrow")}
    eng.dispose()


def test_cli_guards_live_db(tmp_path, monkeypatch, capsys):
    live = tmp_path / "live" / "easy_week.db"
    live.parent.mkdir()
    build_prod_like(live)
    monkeypatch.setattr(migrations.settings, "db_path", str(live))
    before = sha(live)
    # --db на файл живой базы — отказ; без --db/--live — отказ
    assert cli.main(["apply", "--db", str(live)]) == 2
    assert cli.main(["apply"]) == 2
    # --live при запущенном сервисе — отказ, база не тронута
    assert cli.main(["apply", "--live"], is_active=lambda: True) == 2
    assert cli.main(["sync", "--live"], is_active=lambda: True) == 2
    assert cli.main(["apply", "--live", "--no-backup"], is_active=lambda: False) == 2
    assert sha(live) == before and "запущен" in capsys.readouterr().err
    # чтение — можно и при запущенном
    assert cli.main(["status", "--live"], is_active=lambda: True) == 0
    assert sha(live) == before
    # сервис остановлен — своя копия (integrity ok) и миграция
    assert cli.main(["apply", "--live"], is_active=lambda: False) == 0
    backups = list((live.parent / "backups").glob("easy_week-pre-recipes_v1-*.db"))
    assert len(backups) == 1 and migrations.integrity_ok(backups[0]) == "ok"
    assert q(backups[0], "SELECT count(*) FROM planrow") == [(9,)]
    assert not inspect(_engine(backups[0])).has_table("recipe")  # копия — до миграции
    assert cli.main(["verify", "--live", "--against", str(backups[0])]) == 0
    # следующий деплой: только sync — своя копия под своим именем, копия миграции на месте
    assert cli.main(["apply", "--live"], is_active=lambda: False) == 0
    assert len(list((live.parent / "backups").glob("easy_week-pre-recipes_sync-*.db"))) == 1
    assert list((live.parent / "backups").glob("easy_week-pre-recipes_v1-*.db")) == backups
    # репетиция по живой базе читает её только на чтение
    after = sha(live)
    assert cli.main(["rehearse"]) == 0
    assert sha(live) == after


def _prod_checkout(root: Path) -> Path:
    """Каталог backend «прода» вне текущего: app/, .env с относительным DB_PATH, база."""
    backend = root / "prod" / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "main.py").write_text("", encoding="utf-8")
    (backend / ".env").write_text("APP_PASSWORD=x\nDB_PATH=data/easy_week.db\n",
                                  encoding="utf-8")
    db = backend / "data" / "easy_week.db"
    db.parent.mkdir()
    build_prod_like(db)
    return db


def test_cli_guard_live_db_from_another_checkout(tmp_path, monkeypatch, capsys):
    """Агент в соседней рабочей копии (там нет .env, «живая база» по cwd — другой файл) с
    --db на базу прода: отказ — по каталогу бэкенда вокруг файла и по unit-файлу сервиса."""
    live = _prod_checkout(tmp_path)
    before = sha(live)
    other = tmp_path / "worktree" / "backend"
    other.mkdir(parents=True)
    monkeypatch.chdir(other)
    monkeypatch.setattr(migrations.settings, "db_path", "data/easy_week.db")  # как без .env
    assert migrations.live_db_path() == (other / "data" / "easy_week.db").resolve()
    for cmd in (["apply", "--db", str(live), "--no-backup"], ["sync", "--db", str(live)],
                ["status", "--db", str(live)]):
        assert cli.main(cmd, is_active=lambda: False) == 2
    assert "база приложения из" in capsys.readouterr().err
    assert sha(live) == before

    # unit сервиса (WorkingDirectory + EnvironmentFile): база вне каталога бэкенда
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    elsewhere = tmp_path / "srv" / "ew.db"
    elsewhere.parent.mkdir()
    build_prod_like(elsewhere)
    env = tmp_path / "srv" / "service.env"
    env.write_text(f"DB_PATH={elsewhere}\n", encoding="utf-8")
    (unit_dir / "easy-week-backend.service").write_text(
        f"[Service]\nWorkingDirectory={tmp_path / 'srv'}\nEnvironmentFile=-{env}\n",
        encoding="utf-8")
    monkeypatch.setattr(migrations, "UNIT_DIRS", (unit_dir,))
    assert elsewhere.resolve() in migrations.service_db_paths()
    assert cli.main(["apply", "--db", str(elsewhere), "--no-backup"]) == 2
    assert "база сервиса" in capsys.readouterr().err
    # обычная копия — можно
    copy = tmp_path / "copies" / "copy.db"
    migrations.copy_db(live, copy)
    assert cli.main(["apply", "--db", str(copy), "--no-backup"]) == 0


def test_cli_refuses_writes_while_db_open_elsewhere(db, capsys):
    """Файл базы держит другой процесс (сервис, dev-uvicorn, sqlite3) — пишущие команды
    отказываются (код 2), чтение можно."""
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sqlite3, sys, time\n"
         "c = sqlite3.connect(sys.argv[1]); c.execute('SELECT count(*) FROM planrow')\n"
         "print('ready', flush=True); time.sleep(60)\n", str(db)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        before = sha(db)
        assert holder.pid in migrations.open_by_others(db)
        assert cli.main(["apply", "--db", str(db), "--no-backup"]) == 2
        assert "открыт другими процессами" in capsys.readouterr().err
        assert sha(db) == before
        assert cli.main(["status", "--db", str(db)]) == 0
    finally:
        holder.kill()
        holder.wait()
    assert cli.main(["apply", "--db", str(db), "--no-backup"]) == 0


def test_apply_sync_failure_exit_code(db, monkeypatch, capsys):
    """apply: шаг закоммичен, а sync упал — код 3 (не «база не изменена»): маркер есть, sync
    откачен. Следующий деплой (шаг уже есть) с тем же сбоем — тоже 3."""
    def broken(session, **kw):
        raise recipes_v1.MigrationError("sync сломался")

    monkeypatch.setattr(recipes_v1, "sync_session", broken)
    assert cli.main(["apply", "--db", str(db), "--no-backup"]) == 3
    assert "только что применён" in capsys.readouterr().err
    assert q(db, "SELECT name FROM schema_migration") == [("recipes_v1",)]
    assert cli.main(["apply", "--db", str(db), "--no-backup"]) == 3
    assert "применён раньше" in capsys.readouterr().err
    monkeypatch.undo()
    assert cli.main(["apply", "--db", str(db), "--no-backup"]) == 0


def test_service_check_reports_unknown_as_active(monkeypatch):
    def no_systemctl(*a, **kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(migrations.subprocess, "run", no_systemctl)
    assert migrations.service_active() is True


def test_immutable_trigger(db):
    cli.run_apply(db, backup=False)
    eng = _engine(db)
    with eng.begin() as c:
        c.execute(text("UPDATE recipe_revision SET hidden_at = '2026-10-01'"))  # можно
    with pytest.raises(Exception, match="immutable"):
        with eng.begin() as c:
            c.execute(text("UPDATE recipe_revision SET steps = '[]'"))
    eng.dispose()


def test_models_registered_only_in_recipe_metadata():
    assert set(RECIPE_METADATA.tables) == {"recipe", "recipe_revision", "schema_migration"}
    assert not set(RECIPE_METADATA.tables) & set(SQLModel.metadata.tables)
    assert Recipe.__tablename__ == "recipe" and RecipeRevision.__tablename__ == "recipe_revision"
    cols = set(RecipeRevision.__table__.columns.keys())
    assert {"hidden_at", "gen_id", "ctx_uses", "source_used", "model_ref", "kind", "change",
            "parent_id", "content_hash", "meta_hash", "created_at", "created_at_estimated",
            "generated_at"} <= cols
