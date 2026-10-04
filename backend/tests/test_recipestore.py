"""services/recipestore: хэши и id, чтение из таблиц (паритет с JSON), Книга по таблицам,
двойная запись из planstore (в т.ч. сбой в SAVEPOINT), ссылки оценок/избранного/реплик.

Базы — с миграцией (conftest.migrate: таблицы + маркер), кроме проверки «без маркера»."""

import asyncio
import copy
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel, select

from app.config import settings as config
from app.migrations import __main__ as cli
from app.migrations import recipes_v1
from app.models import Conversation, MessageRow, PlanRow, Recipe, RecipeRevision
from app.routers import discuss as discuss_router
from app.routers import ratings as ratings_router
from app.schemas import DiscussRequest, RatingBody
from app.services import planstore, recipestore as rs
from app.services.variants import apply_variant, with_detail
from tests.conftest import migrate
from tests.test_recipe_migration import _engine, flat, header, var, vdish

DET = {"ingredients": [{"name": "говядина", "qty": 500, "unit": "г", "category": "Мясо и птица"}],
       "steps": ["Туши"], "tips": [], "note": "", "provider": "DeepSeek", "model": "deepseek",
       "model_ref": "deepseek:deepseek-chat", "gen_id": "g1"}


@pytest.fixture()
def eng(tmp_path):
    """Файловая база с миграцией рецептов (отдельные сессии = отдельные запросы)."""
    e = _engine(tmp_path / "rs.db")
    SQLModel.metadata.create_all(e)
    migrate(e)
    yield e
    e.dispose()


def _new_plan(s: Session, pid: str, dishes: list[dict], *, conv="c1", status="draft",
              parent=None, decided=None) -> PlanRow:
    if s.get(Conversation, conv) is None:
        s.add(Conversation(id=conv))
    row = planstore.new_row(s, id=pid, conversation_id=conv, title=pid, week_label="1–7",
                            status=status, parent_id=parent, decided_at=decided, dishes=dishes)
    s.commit()
    return row


def _dish(e, pid, did) -> dict:
    with Session(e) as s:
        return next(d for d in s.get(PlanRow, pid).dishes if d["id"] == did)


def _count(e, model) -> int:
    with Session(e) as s:
        return len(s.exec(select(model)).all())


# --- хэши и id ---


def test_content_hash_is_content_only():
    v = var("DeepSeek", "шаг")
    meta = {**v, "model_ref": "deepseek:x", "kind": "generate", "gen_id": "g", "ctx_uses": ["лук"]}
    assert rs.content_hash(v) == rs.content_hash(meta)
    reordered = {**v, "ingredients": [dict(reversed(list(i.items()))) for i in v["ingredients"]]}
    assert rs.content_hash(v) == rs.content_hash(reordered)
    assert rs.content_hash(v) != rs.content_hash({**v, "steps": ["другой"]})
    # отсутствующее поле = пустое (как читает apply_variant)
    assert rs.content_hash({"steps": ["a"]}) == rs.content_hash(
        {"steps": ["a"], "ingredients": None, "tips": [], "note": None})
    assert len({rs.meta_hash("A", "t", ""), rs.meta_hash("B", "t", ""),
                rs.meta_hash("A", "t2", ""), rs.meta_hash("A", "t", "src")}) == 4
    rid = rs.recipe_id_for("lin:p/d/борщ")
    assert rid == rs.recipe_id_for("lin:p/d/борщ") and len(rid) == 32
    assert rs.revision_id_for(rid, "deepseek", "h", "m") == rs.revision_id_for(
        rid, "deepseek", "h", "m") != rs.revision_id_for(rid, "gemini", "h", "m")


def test_variants_for_legacy_keeps_generated_at():
    d = flat("d", "Гуляш", "Claude", "гуляш", gen="2026-07-05T09:00:00+00:00")
    assert rs.variants_for(d)["anthropic"]["generated_at"] == "2026-07-05T09:00:00+00:00"
    assert rs.variants_for(header("h", "Шапка")) == {}
    spec = {**header("s", "Спека"), "ingredients": [{"name": "фарш", "qty": 1, "unit": "г",
                                                     "category": "x"}]}
    assert rs.variants_for(spec) == {}  # без провайдера — не рецепт, остаётся в JSON


# --- двойная запись ---


def test_new_row_and_patch_pin_and_keep_history(eng):
    with Session(eng) as s:
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "v1")}),
                            header("d2", "Щи")])
    d = _dish(eng, "p1", "d1")
    assert d["recipe_id"] == rs.recipe_id_for(rs.lineage_key("p1", "d1", "гуляш"))
    first = d["rev_ids"]["deepseek"]
    assert "recipe_id" not in _dish(eng, "p1", "d2")  # шапка — без рецепта

    # «↻» дважды: JSON-вариант перезаписан, а таблица хранит все тексты (история)
    for n, step in enumerate(("v2", "v3")):
        det = {**DET, "steps": [step]}
        with Session(eng) as s:
            planstore.patch_dishes(s, "p1", {"d1": lambda cur, det=det: with_detail(
                cur, "deepseek", det, kind="regenerate", change="острее")})
    d = _dish(eng, "p1", "d1")
    assert d["steps"] == ["v3"] and list(d["rev_ids"]) == ["deepseek"]
    with Session(eng) as s:
        revs = {r.id: r for r in s.exec(select(RecipeRevision)).all()}
    assert len(revs) == 3 and first in revs
    last = revs[d["rev_ids"]["deepseek"]]
    assert (last.kind, last.change, last.model_ref, last.gen_id) == (
        "regenerate", "острее", "deepseek:deepseek-chat", "g1")
    middle = revs[last.parent_id]
    assert middle.steps == ["v2"] and middle.parent_id == first
    # заголовок — шапка шаблона «Щи» — так и не получил рецепт; второй тот же патч — без вставок
    with Session(eng) as s:
        planstore.patch_dishes(s, "p1", {"d1": lambda cur: {**cur, "emoji": "🍖"}})
    assert _count(eng, RecipeRevision) == 3


def test_child_version_inherits_recipe_and_first_open_is_shared(eng):
    """Шапка в v1 и v2 (правка в чате): рецепт, впервые открытый в v2, — линия от v1; открытие
    того же блюда в v1 даёт тот же рецепт (INSERT OR IGNORE, без дублей)."""
    with Session(eng) as s:
        _new_plan(s, "v1", [header("d1", "Плов")])
        _new_plan(s, "v2", [header("d1", "Плов")], parent="v1")
    for pid, step in (("v2", "a"), ("v1", "b")):
        with Session(eng) as s:
            planstore.patch_dishes(s, pid, {"d1": lambda cur, step=step: with_detail(
                cur, "deepseek", {**DET, "steps": [step]}, kind="generate")})
    want = rs.recipe_id_for(rs.lineage_key("v1", "d1", "плов"))
    assert _dish(eng, "v1", "d1")["recipe_id"] == _dish(eng, "v2", "d1")["recipe_id"] == want
    assert _count(eng, Recipe) == 1 and _count(eng, RecipeRevision) == 2


def _open(e, pid: str, did: str, step: str, model: str = "deepseek") -> None:
    """Первое открытие / генерация варианта model у блюда pid/did (как роутер деталей)."""
    with Session(e) as s:
        planstore.patch_dishes(s, pid, {did: lambda cur: with_detail(
            cur, model, {**DET, "steps": [step], "provider": model.title()}, kind="generate")})


def test_rule2_root_deleted_first_open_in_grandchild(eng):
    """v1→v2→v3 (шапка «Плов»): рецепт впервые открыт в v2 (линия от v1), потом v1 удалили
    (DELETE /api/plans/{id} оставляет потомкам висячий parent_id) и блюдо открыли в v3. Правило
    2 (наследуем у ближайшего предка) держит тот же рецепт — правило 4 дало бы lin:v2."""
    with Session(eng) as s:
        _new_plan(s, "v1", [header("d1", "Плов")])
        _new_plan(s, "v2", [header("d1", "Плов")], parent="v1")
        _new_plan(s, "v3", [header("d1", "Плов")], parent="v2")
    _open(eng, "v2", "d1", "a")
    rid = _dish(eng, "v2", "d1")["recipe_id"]
    assert rid == rs.recipe_id_for(rs.lineage_key("v1", "d1", "плов"))
    with Session(eng) as s:
        s.delete(s.get(PlanRow, "v1"))
        s.commit()
    _open(eng, "v3", "d1", "b")
    assert _dish(eng, "v3", "d1")["recipe_id"] == rid and _count(eng, Recipe) == 1


def test_deep_first_open_same_ids_after_strip_drop_apply(tmp_path):
    """Первое открытие в версии на 2 уровня ниже корня: линия — от самого старого предка (как у
    миграции, которая обходит всю цепочку), поэтому strip + drop + apply дают те же id."""
    path = tmp_path / "deep.db"
    e = _engine(path)
    SQLModel.metadata.create_all(e)
    migrate(e)
    with Session(e) as s:
        _new_plan(s, "v1", [header("d1", "Плов")])
        _new_plan(s, "v2", [header("d1", "Плов")], parent="v1")
        _new_plan(s, "v3", [header("d1", "Плов")], parent="v2")
    _open(e, "v3", "d1", "a")
    before = _dish(e, "v3", "d1")
    e.dispose()
    assert before["recipe_id"] == rs.recipe_id_for(rs.lineage_key("v1", "d1", "плов"))
    cli._tx(path, recipes_v1.strip_session, name="strip")
    cli._tx(path, recipes_v1.drop_session, name="drop")
    cli.run_apply(path, backup=False)
    e = _engine(path)
    after = _dish(e, "v3", "d1")
    e.dispose()
    assert (after["recipe_id"], after["rev_ids"]) == (before["recipe_id"], before["rev_ids"])


def test_new_model_variant_parent_is_active_pin(eng):
    """Выбор второй модели при активной первой (select): у новой версии parent_id — закрепление
    активной модели (parent_id варианта — ключ слота, а не та же модель)."""
    with Session(eng) as s:
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "a")})])
    ds_pin = _dish(eng, "p1", "d1")["rev_ids"]["deepseek"]
    _open(eng, "p1", "d1", "gemini-текст", model="gemini")
    d = _dish(eng, "p1", "d1")
    assert list(d["rev_ids"]) == ["deepseek", "gemini"] and d["rev_ids"]["deepseek"] == ds_pin
    with Session(eng) as s:
        assert s.get(RecipeRevision, d["rev_ids"]["gemini"]).parent_id == ds_pin


def test_runtime_estimated_date_is_plan_date(eng):
    """Текст без generated_at (legacy-деталь), записанный двойной записью: дата версии — дата
    версии плана (как у миграции), «≈»; не момент записи."""
    with Session(eng) as s:
        row = _new_plan(s, "p1", [flat("d1", "Суп", "DeepSeek", "суп")])
        created = rs._naive_utc(row.created_at)
    with Session(eng) as s:
        rev = s.get(RecipeRevision, _dish(eng, "p1", "d1")["rev_ids"]["deepseek"])
        assert rev.created_at_estimated and rev.created_at == created
        assert s.get(Recipe, rev.recipe_id).created_at == created


def test_patch_of_pinned_plan_skips_chain_and_per_variant_queries(eng):
    """Обычная запись уже закреплённых блюд: ни JSON предков (цепочка — лениво, только для
    блюда без recipe_id), ни запроса на каждый вариант (версии рецептов — одним IN)."""
    from sqlalchemy import event

    variants = {m: var(m.title(), f"{m}-текст") for m in ("deepseek", "gemini", "anthropic")}
    dishes = [vdish(f"d{i}", f"Блюдо {i}", copy.deepcopy(variants)) for i in range(7)]
    with Session(eng) as s:
        _new_plan(s, "v0", copy.deepcopy(dishes))
        for i in range(1, 6):
            row = s.get(PlanRow, f"v{i - 1}")
            _new_plan(s, f"v{i}", copy.deepcopy(row.dishes), parent=f"v{i - 1}")
    statements: list[str] = []

    def log(conn, cursor, stmt, *a):
        statements.append(stmt)

    event.listen(eng, "before_cursor_execute", log)
    try:
        with Session(eng) as s:
            planstore.patch_dishes(s, "v5", {"d3": lambda cur: {**cur, "emoji": "🍖"}})
    finally:
        event.remove(eng, "before_cursor_execute", log)
    selects = [st for st in statements if st.lstrip().upper().startswith("SELECT")]
    assert not any("planrow.dishes" in st and "planrow.parent_id" in st for st in selects)
    assert len(selects) <= 8, selects
    assert _count(eng, RecipeRevision) == 21


def test_without_marker_behaves_like_phase0(tmp_path):
    """Код фазы 1 без маркера (миграция не применена / снята) = фаза 0: ни закреплений, ни
    обращений к таблицам рецептов."""
    e = _engine(tmp_path / "plain.db")
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "v1")})])
        planstore.patch_dishes(s, "p1", {"d1": lambda cur: with_detail(
            cur, "gemini", {**DET, "provider": "Gemini"}, kind="generate")})
    assert not any(k in _dish(e, "p1", "d1") for k in rs.PIN_KEYS)
    e.dispose()
    # таблицы есть, а маркера нет (strip снял его) — тоже выключено
    e = _engine(tmp_path / "nomarker.db")
    SQLModel.metadata.create_all(e)
    migrate(e)
    with Session(e) as s:
        s.exec(text("DELETE FROM schema_migration"))
        s.commit()
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "v1")})])
        planstore.patch_dishes(s, "p1", {"d1": lambda cur: {**cur, "emoji": "🍖"}})
    assert "rev_ids" not in _dish(e, "p1", "d1") and _count(e, RecipeRevision) == 0
    e.dispose()


def test_dual_write_failure_keeps_json_and_sync_heals(eng, monkeypatch, caplog):
    with Session(eng) as s:
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "v1")})])
    revs_before = _count(eng, RecipeRevision)
    real = rs.sync_dishes

    def half_then_fail(*a, **kw):
        real(*a, **kw)  # вставки в таблицы уже сделаны — их откатит SAVEPOINT
        raise RuntimeError("таблицы рецептов сломались")

    monkeypatch.setattr(rs, "sync_dishes", half_then_fail)
    errors = rs._sync_total.labels(result="error")._value.get()
    caplog.set_level(logging.ERROR, logger="easy_week.recipes")
    with Session(eng) as s:
        out = planstore.patch_dishes(s, "p1", {"d1": lambda cur: with_detail(
            cur, "deepseek", {**DET, "steps": ["v2"]}, kind="regenerate")})
    d = _dish(eng, "p1", "d1")
    assert out[0]["steps"] == d["steps"] == ["v2"]  # JSON записан
    with Session(eng) as s:
        assert s.get(PlanRow, "p1").dishes_version == 1
    assert _count(eng, RecipeRevision) == revs_before  # частичные вставки откачены
    assert rs.content_hash(d["variants"]["deepseek"]) != rs.content_hash(var("DeepSeek", "v1"))
    assert rs._sync_total.labels(result="error")._value.get() == errors + 1
    assert any("двойная запись" in r.getMessage() for r in caplog.records)

    # новый план с той же поломкой — строка и реплика в той же транзакции сохраняются
    with Session(eng) as s:
        s.add(Conversation(id="c9"))
        planstore.new_row(s, id="p9", conversation_id="c9", title="t", week_label="w",
                          dishes=[vdish("x", "Икс", {"deepseek": var("DeepSeek", "x")})])
        s.add(MessageRow(id="m9", conversation_id="c9", role="assistant", text="план",
                         plan_id="p9"))
        s.commit()
    assert "rev_ids" not in _dish(eng, "p9", "x")
    with Session(eng) as s:
        assert s.get(MessageRow, "m9") is not None

    # следующий деплой: sync закрепляет всё, что двойная запись пропустила
    monkeypatch.setattr(rs, "sync_dishes", real)
    with Session(eng) as s:
        res = recipes_v1.sync_session(s)
        s.commit()
    assert res["verify"]["ok"] and res["revisions_new"] == 2 and res["kinds_new"] == {
        "regenerate": 1, "resync": 1}
    assert _dish(eng, "p1", "d1")["rev_ids"] and _dish(eng, "p9", "x")["rev_ids"]


def test_own_recipe_note_repins_other_variants(eng):
    """↻ с уточнением своего рецепта дописывает source: другие варианты того же блюда (текст
    тот же) получают версию с новым source (правило ничьей) — закрепления сходятся с JSON."""
    own = vdish("own-0-x", "Блины", {"deepseek": var("DeepSeek", "a"),
                                     "anthropic": var("Claude", "b")}, source="мой текст")
    with Session(eng) as s:
        if s.get(Conversation, "library") is None:
            s.add(Conversation(id="library"))
        planstore.new_row(s, id="library", conversation_id="library", title="Мои рецепты",
                          week_label="", status="library", dishes=[own])
        s.commit()
    before = _dish(eng, "library", "own-0-x")["rev_ids"]

    def note(cur):
        new = with_detail(cur, "deepseek", {**DET, "steps": ["тоньше"]}, kind="regenerate",
                          change="тоньше")
        new["source"] = f"{new['source']}\n\nУточнение: тоньше"
        return new

    with Session(eng) as s:
        planstore.patch_dishes(s, "library", {"own-0-x": note})
    after = _dish(eng, "library", "own-0-x")["rev_ids"]
    assert after["deepseek"] != before["deepseek"] and after["anthropic"] != before["anthropic"]
    with Session(eng) as s:
        r = s.get(RecipeRevision, after["anthropic"])
        assert r.steps == ["b"] and r.source_used.endswith("Уточнение: тоньше")
        assert s.get(Recipe, r.recipe_id).source == "мой текст"  # исходный текст неизменен
        assert recipes_v1.verify_session(s, strict_refs=False)["ok"]


# --- чтение из таблиц ---


def test_hydrate_rows_parity(eng):
    dishes = [
        vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "a"), "gemini": var("Gemini", "b")},
              active="gemini", uses=["лук"]),
        flat("d2", "Суп", "Claude", "суп", gen="2026-07-05T09:00:00+00:00"),
        flat("d3", "Рис", "DeepSeek", "рис"),
        vdish("d4", "Свой", {"anthropic": var("Claude", "c")}, source="текст", from_book=True),
        header("d5", "Шапка"),
    ]
    with Session(eng) as s:
        _new_plan(s, "p1", copy.deepcopy(dishes))
        row = s.get(PlanRow, "p1")
        stripped = [recipes_v1._strip_body(d) if d.get("rev_ids") else d for d in row.dishes]
        hyd = rs.hydrate_rows(s, [type("R", (), {"id": "p1", "dishes": stripped})()])["p1"]
    for orig, h in zip(dishes, hyd):
        if rs.variants_for(orig):
            assert recipes_v1._norm(h) == recipes_v1._norm(recipes_v1._legacy_form(orig))
        else:
            assert h == orig  # шапка — как в JSON
    assert hyd[1]["detail_generated_at"] == "2026-07-05T09:00:00+00:00"
    assert hyd[1]["active_model"] == "anthropic"  # legacy: единственная закреплённая модель
    assert hyd[3]["source"] == "текст" and hyd[3]["from_book"] is True
    assert "source" not in hyd[0]
    assert hyd[0]["variants"]["deepseek"]["revision_id"]


def test_hydrate_miss_serves_json(caplog):
    d = vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "a")})
    d = {**d, "recipe_id": "r", "rev_ids": {"deepseek": "нет-такой"}}
    misses = rs._hydrate_miss._value.get()
    caplog.set_level(logging.ERROR, logger="easy_week.recipes")
    assert rs.hydrate_dish(d, {}) == d
    assert rs._hydrate_miss._value.get() == misses + 1
    assert any("нет-такой" in r.getMessage() for r in caplog.records)


def test_book_entries_follow_accepted_plans(eng):
    from datetime import datetime, timezone

    day = lambda n: datetime(2026, 9, n, tzinfo=timezone.utc)  # noqa: E731
    with Session(eng) as s:
        _new_plan(s, "old", [vdish("d1", "Борщ", {"deepseek": var("DeepSeek", "old")})],
                  conv="c1", status="accepted", decided=day(1))
        _new_plan(s, "acc", [vdish("d1", "Плов", {"deepseek": var("DeepSeek", "plov")}),
                             header("d2", "Шапка")],
                  conv="c2", status="accepted", decided=day(5))
        # черновик-потомок принятого: ↻ здесь Книгу не меняет
        _new_plan(s, "draft", [vdish("d1", "Плов", {"deepseek": var("DeepSeek", "plov")})],
                  conv="c2", parent="acc")
        _new_plan(s, "solo", [vdish("d1", "Только черновик", {"deepseek": var("DeepSeek", "x")})],
                  conv="c3")
        s.add(Conversation(id="library"))
        planstore.new_row(s, id="library", conversation_id="library", title="Мои рецепты",
                          week_label="", status="library",
                          dishes=[vdish("own-0-b", "Блины", {"deepseek": var("DeepSeek", "b")},
                                        source="мой")])
        s.commit()
    with Session(eng) as s:
        planstore.patch_dishes(s, "draft", {"d1": lambda cur: with_detail(
            cur, "deepseek", {**DET, "steps": ["черновой ↻"]}, kind="regenerate")})
    with Session(eng) as s:
        entries = rs.book_entries(s)
        assert [(e.plan_id, e.dish_id, e.origin) for e in entries] == [
            ("library", "own-0-b", "own"), ("acc", "d1", "plan"), ("old", "d1", "plan")]
        plov = entries[1]
        assert plov.dish["steps"] == ["plov"]  # не черновой ↻
        assert plov.recipe_id == _dish(eng, "draft", "d1")["recipe_id"]  # тот же рецепт
        idx = rs.book_index_tables(s)
        assert list(idx) == ["блины", "плов", "борщ"]
        assert recipes_v1.verify_session(s, strict_refs=False)["checks"]["V8_book"]["ok"]


# --- ссылки: оценки, избранное, реплики обсуждения ---


def _rate(e, model: str, vote: int, plan_id="p1", dish_id="d1"):
    with Session(e) as s:
        return asyncio.run(ratings_router.rate(RatingBody(
            target_type="recipe", target_id=dish_id, model=model, vote=vote, plan_id=plan_id,
            dish_id=dish_id), s))


def _rating_ref(e, model: str) -> tuple:
    with Session(e) as s:
        return tuple(s.exec(text("SELECT recipe_id, revision_id FROM ratingrow WHERE model = :m"),
                            params={"m": model}).one())


def test_rating_after_failed_dual_write_is_not_linked_to_stale_pin(eng, monkeypatch):
    """Двойная запись ↻ упала: в JSON новый текст, а rev_ids — прежняя версия. Голос за новый
    текст не должен лечь на старую версию (sync непустые ссылки не правит): ссылка пустая
    (и сменённый голос обнуляет прежнюю), sync перезакрепляет и дописывает ту версию, что
    видел пользователь."""
    with Session(eng) as s:
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "v1"),
                                                  "gemini": var("Gemini", "g1")})])
    pins = _dish(eng, "p1", "d1")["rev_ids"]
    _rate(eng, "gemini", 1)  # голос до сбоя — за закреплённый текст
    assert _rating_ref(eng, "gemini") == (_dish(eng, "p1", "d1")["recipe_id"], pins["gemini"])

    def broken(*a, **kw):
        raise RuntimeError("таблицы рецептов сломались")

    monkeypatch.setattr(rs, "sync_dishes", broken)
    for model in ("deepseek", "gemini"):
        with Session(eng) as s:
            planstore.patch_dishes(s, "p1", {"d1": lambda cur, m=model: with_detail(
                cur, m, {**DET, "steps": [f"{m}-v2"], "provider": m.title()},
                kind="regenerate")})
    stale = _dish(eng, "p1", "d1")
    assert stale["rev_ids"] == pins and stale["variants"]["deepseek"]["steps"] == ["deepseek-v2"]
    _rate(eng, "deepseek", -1)  # новый голос — за новый текст
    _rate(eng, "gemini", -1)  # сменённый голос (та же строка) — прежняя ссылка обнуляется
    assert _rating_ref(eng, "deepseek") == (None, None) == _rating_ref(eng, "gemini")

    monkeypatch.undo()  # таблицы починили — следующий деплой: sync
    with Session(eng) as s:
        res = recipes_v1.sync_session(s)
        s.commit()
    healed = _dish(eng, "p1", "d1")
    assert res["verify"]["ok"] and res["repinned"] == 1 and healed["rev_ids"] != pins
    for model in ("deepseek", "gemini"):
        assert _rating_ref(eng, model) == (healed["recipe_id"], healed["rev_ids"][model])
        with Session(eng) as s:
            rev = s.get(RecipeRevision, healed["rev_ids"][model])
            assert rev.steps == [f"{model}-v2"]
    with Session(eng) as s:
        strict = recipes_v1.verify_session(s, strict_refs=True)
    assert strict["ok"], strict["failed"]


def test_rating_and_favorite_record_recipe_and_revision(monkeypatch):
    from app.db import engine as app_engine
    from app.main import app

    monkeypatch.setattr(config, "app_password", "")
    with Session(app_engine) as s:
        _new_plan(s, "ref-p1", [vdish("d1", "Солянка", {
            "deepseek": var("DeepSeek", "a"), "anthropic": var("Claude", "b")})], conv="ref-c1")
    d = _dish(app_engine, "ref-p1", "d1")
    with TestClient(app) as c:
        r = c.post("/api/ratings", json={"targetType": "recipe", "targetId": "d1",
                                         "model": "anthropic", "vote": 1, "planId": "ref-p1",
                                         "dishId": "d1"})
        assert r.status_code == 200 and r.json()["vote"] == 1
        f = c.put("/api/recipes/favorite", json={"name": "Солянка", "favorite": True,
                                                  "planId": "ref-p1", "dishId": "d1"})
        assert f.status_code == 200
        health = c.get("/api/health").json()
    assert health["recipes"]["marker"] is True and health["recipes"]["store"] == "json"
    with Session(app_engine) as s:
        rat = s.exec(text("SELECT recipe_id, revision_id FROM ratingrow WHERE plan_id = 'ref-p1'"
                          )).one()
        fav = s.exec(text("SELECT recipe_id FROM favoriterecipe WHERE key = 'солянка'")).one()
        s.exec(text("DELETE FROM favoriterecipe WHERE key = 'солянка'"))
        s.exec(text("DELETE FROM ratingrow WHERE plan_id = 'ref-p1'"))
        s.delete(s.get(PlanRow, "ref-p1"))
        s.commit()
    assert tuple(rat) == (d["recipe_id"], d["rev_ids"]["anthropic"])
    assert fav[0] == d["recipe_id"]


def test_discussion_messages_record_recipe(session, monkeypatch):
    from tests.test_discuss import DETAIL, FakeGate, _use

    _new_plan(session, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "a")}),
                              header("d2", "Щи")])
    rid = session.get(PlanRow, "p1").dishes[0]["recipe_id"]
    _use(monkeypatch, FakeGate([DETAIL], tools=[{"name": "update_recipe",
                                                 "args": {"change": "говядина"}}]))
    asyncio.run(discuss_router.chat_discuss(DiscussRequest(
        plan_id="p1", target="recipe", dish_id="d1", message="Сделай на говядине",
        recipe_model="fake"), session))
    _use(monkeypatch, FakeGate([{"reply": "Можно.", "action": {"op": "none"}}]))
    asyncio.run(discuss_router.chat_discuss(DiscussRequest(
        plan_id="p1", target="recipe", dish_id="d2", message="А щи?", recipe_model="fake"),
        session))
    rows = session.exec(text(
        "SELECT dish_id, role, recipe_id FROM messagerow WHERE discuss_target = 'recipe' "
        "ORDER BY rowid")).all()
    assert [tuple(r) for r in rows] == [("d1", "user", rid), ("d1", "assistant", rid),
                                        ("d2", "user", None), ("d2", "assistant", None)]
    # правка из обсуждения — новая версия того же рецепта (kind discuss_edit)
    d1 = session.get(PlanRow, "p1").dishes[0]
    rev = session.get(RecipeRevision, d1["rev_ids"]["fake"])
    assert d1["recipe_id"] == rid and (rev.kind, rev.change) == ("discuss_edit", "говядина")


def test_discuss_edit_of_header_dish_links_new_recipe(session, monkeypatch):
    """Обсуждение блюда-шапки с правкой: у блюда впервые появляется рецепт — реплика бота
    получает его id, реплика пользователя (до правки рецепта не было) — пустая, её дописывает
    sync (по свежей версии плана беседы)."""
    from tests.test_discuss import DETAIL, FakeGate, _use

    _new_plan(session, "p1", [header("d2", "Щи")])
    _use(monkeypatch, FakeGate([DETAIL], tools=[{"name": "update_recipe",
                                                 "args": {"change": "на говядине"}}]))
    out = asyncio.run(discuss_router.chat_discuss(DiscussRequest(
        plan_id="p1", target="recipe", dish_id="d2", message="Сделай на говядине",
        recipe_model="fake"), session))
    assert out.op == "edit"
    rid = session.get(PlanRow, "p1").dishes[0]["recipe_id"]
    rows = session.exec(text(
        "SELECT role, recipe_id FROM messagerow WHERE discuss_target = 'recipe' ORDER BY rowid"
    )).all()
    assert [tuple(r) for r in rows] == [("user", None), ("assistant", rid)]
    res = recipes_v1.sync_session(session)
    session.commit()
    assert res["refs_filled"] == {"messages": 1}
    assert {r for (r,) in session.exec(text(
        "SELECT recipe_id FROM messagerow WHERE discuss_target = 'recipe'")).all()} == {rid}


def test_startup_check_reports_verify(eng):
    with Session(eng) as s:
        _new_plan(s, "p1", [vdish("d1", "Гуляш", {"deepseek": var("DeepSeek", "a")})])
    state = rs.startup_check(eng)
    assert state["marker"] and state["verify"]["ok"] and rs.health()["store"] == "json"
    # расхождение (запись мимо двойной записи) — видно в health, приложение работает дальше
    with Session(eng) as s:
        row = s.get(PlanRow, "p1")
        bad = [apply_variant(row.dishes[0], "deepseek", {"deepseek": var("DeepSeek", "z")})]
        s.exec(text("UPDATE planrow SET dishes = :d WHERE id = 'p1'"),
               params={"d": __import__("json").dumps(bad)})
        s.commit()
    state = rs.startup_check(eng)
    assert state["marker"] and not state["verify"]["ok"]
    assert "V3_pins" in state["verify"]["failed"]
