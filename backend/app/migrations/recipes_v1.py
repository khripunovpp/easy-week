"""Шаг recipes_v1: таблицы рецептов, закрепления блюд, ссылки оценок/избранного/реплик.

Всё — в одной транзакции вызывающего (app/migrations/__main__.py, BEGIN IMMEDIATE), коммит —
только после сверки V1–V10. Правила закрепления — те же, что у двойной записи в приложении
(services/recipestore.pin_dish), поэтому миграция, `sync` и живая запись дают одни и те же id.

Как сохраняем данные прода:
- ни одна строка бесед, планов, сообщений, оценок, избранного не удаляется и не меняется (кроме
  колонок ссылок на рецепт); id планов и блюд не меняются; прежние ключи блюд в JSON не трогаем —
  блюдо только ПОЛУЧАЕТ recipe_id и rev_ids (V2 сверяет каждое блюдо и отпечатки всех этих строк
  со снимком до записи);
- версии планов обходим от старых к новым: текст, переживший «↻» только в отклонённых
  версиях, тоже становится версией рецепта;
- id детерминированы (uuid5): повторный apply ничего не вставляет, drop + apply — те же id.
"""

import copy
import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import func, text, update
from sqlalchemy import select as sa_select  # Core: строки, а не скаляры (sqlmodel.select)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from ..models import (
    RECIPE_METADATA,
    RECIPE_REF_COLUMNS,
    PlanRow,
    Recipe,
    RecipeRevision,
    SchemaMigration,
)
from ..services import recipestore as rs
from ..services.history import norm_name
from ..services.mapping import to_week_plan
from ..services.recipebook import book_index, book_names, book_rows
from ..services.regenerate import cook_sig, shopping_base
from ..services.variants import apply_variant
from . import MigrationError

logger = logging.getLogger("easy_week.migrations")

STEP = rs.MARKER  # 'recipes_v1'

# Версия неизменна: текст и его идентичность править нельзя, скрыть (hidden_at) — можно.
TRIGGER_SQL = (
    "CREATE TRIGGER IF NOT EXISTS recipe_revision_immutable "
    "BEFORE UPDATE OF recipe_id, model, ingredients, steps, tips, note, content_hash, "
    "meta_hash, source_used ON recipe_revision "
    "BEGIN SELECT RAISE(ABORT, 'recipe_revision is immutable'); END"
)

_LIBRARY = "library"


class VerifyFailed(MigrationError):
    """Сверка до COMMIT не прошла — транзакция откатывается."""

    def __init__(self, report: dict):
        super().__init__("сверка не прошла: " + ", ".join(report.get("failed") or []))
        self.report = report


# --- чтение базы ---


@dataclass
class Row:
    """Версия плана: метаданные + блюда (как в JSON) + сырой текст JSON."""

    id: str
    conversation_id: str
    parent_id: str | None
    status: str
    title: str
    week_label: str
    provider: str
    leftovers: list | None
    shopping_sig: str
    shopping_at: datetime | None
    shopping_model: str
    cooking_plan: dict
    created_at: datetime
    decided_at: datetime | None
    dishes: list[dict]
    raw: str | None = None

    @property
    def library(self) -> bool:
        return self.id == _LIBRARY or self.status == _LIBRARY

    def ns(self, dishes: list[dict] | None = None) -> SimpleNamespace:
        """Объект «как PlanRow» для mapping.to_week_plan / shopping_base / cook_sig."""
        return SimpleNamespace(
            id=self.id, conversation_id=self.conversation_id, title=self.title,
            week_label=self.week_label, status=self.status, provider=self.provider,
            dishes=self.dishes if dishes is None else dishes, leftovers=self.leftovers,
            created_at=self.created_at, shopping_at=self.shopping_at,
            shopping_model=self.shopping_model,
        )


def table_exists(session: Session, name: str) -> bool:
    return session.exec(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"), params={"n": name}
    ).first() is not None


def columns(session: Session, table_name: str) -> set[str]:
    return {r[1] for r in session.exec(text(f'PRAGMA table_info("{table_name}")')).all()}


def marker(session: Session, name: str = STEP) -> dict | None:
    if not table_exists(session, "schema_migration"):
        return None
    got = session.exec(
        sa_select(SchemaMigration.__table__).where(SchemaMigration.__table__.c.name == name)
    ).first()
    return dict(got._mapping) if got is not None else None


def load_rows(session: Session) -> list[Row]:
    """Все версии планов (включая «Мои рецепты») по (created_at, id) — от старых к новым."""
    t = PlanRow.__table__
    raw = dict(session.exec(text("SELECT id, dishes FROM planrow")).all())
    out = [
        Row(
            id=r.id, conversation_id=r.conversation_id, parent_id=r.parent_id, status=r.status,
            title=r.title, week_label=r.week_label, provider=r.provider or "",
            leftovers=r.leftovers, shopping_sig=r.shopping_sig or "",
            shopping_at=r.shopping_at, shopping_model=r.shopping_model or "",
            cooking_plan=r.cooking_plan or {}, created_at=r.created_at,
            decided_at=r.decided_at, dishes=list(r.dishes or []), raw=raw.get(r.id),
        )
        for r in session.exec(sa_select(t)).all()
    ]
    out.sort(key=lambda r: (r.created_at, r.id))
    return out


def load_store(session: Session) -> rs.MemStore:
    """Уже записанные рецепты и версии — в память (в порядке создания)."""
    rt, vt = Recipe.__table__, RecipeRevision.__table__
    recipes = {
        r.id: dict(r._mapping)
        for r in session.exec(sa_select(rt).order_by(rt.c.created_at, rt.c.key)).all()
    }
    revisions = {
        r.id: dict(r._mapping)
        for r in session.exec(sa_select(vt).order_by(vt.c.created_at, vt.c.id)).all()
    }
    return rs.MemStore(recipes=recipes, revisions=revisions)


# --- сборка: чистая функция от версий планов и уже записанного ---


@dataclass
class Build:
    store: rs.MemStore
    pinned: dict[str, list[dict]]
    changed: list[str]
    stats: dict = field(default_factory=dict)


def build(rows: list[Row], store: rs.MemStore, *, default_kind: str) -> Build:
    """Закрепить блюда всех версий (от старых к новым: предок закреплён раньше потомка, и
    потомок наследует его рецепт). Только в памяти — пишет write()."""
    by_id = {r.id: r for r in rows}
    pinned: dict[str, list[dict]] = {}
    for r in rows:
        chain: list[tuple[str, list[dict]]] = []
        seen, pid = {r.id}, r.parent_id
        while pid and pid in by_id and pid not in seen:
            seen.add(pid)
            chain.append((pid, pinned.get(pid, by_id[pid].dishes)))
            pid = by_id[pid].parent_id
        plan = rs.PlanCtx(id=r.id, conversation_id=r.conversation_id, library=r.library,
                          created_at=r.created_at)
        pinned[r.id] = rs.pin_dishes(store, plan, copy.deepcopy(r.dishes), chain,
                                     default_kind=default_kind, at=r.created_at)
    changed = [r.id for r in rows if pinned[r.id] != r.dishes]
    b = Build(store=store, pinned=pinned, changed=changed)
    b.stats = _stats(rows, b)
    return b


def _stats(rows: list[Row], b: Build) -> dict:
    st = b.store
    cats = Counter()
    repinned = linked = book_linked = 0
    by_row = {r.id: r for r in rows}
    for r in rows:
        before = {d.get("id"): d for d in r.dishes}
        for d in b.pinned[r.id]:
            if rs.variants_for(d):
                cats["pinned"] += 1
                old = before.get(d.get("id")) or {}
                if not old.get("rev_ids"):
                    linked += 1
                elif (old.get("recipe_id"), old.get("rev_ids")) != (d["recipe_id"], d["rev_ids"]):
                    repinned += 1
                rec = st.recipes.get(d["recipe_id"]) or {}
                if d.get("from_book") and rec.get("conversation_id") != r.conversation_id:
                    book_linked += 1
            elif d.get("ingredients") or d.get("steps"):
                cats["plan_owned"] += 1
            else:
                cats["header_only"] += 1
    revs = st.revisions.values()
    accepted_pins = {d.get("recipe_id") for r in rows if r.status == "accepted"
                     for d in b.pinned[r.id] if d.get("recipe_id")}
    own = {rid for rid, rec in st.recipes.items() if rec.get("origin") == "own"}
    return {
        "planrows": len(rows), "dishes": sum(len(r.dishes) for r in rows),
        "pinned": cats["pinned"], "plan_owned": cats["plan_owned"],
        "header_only": cats["header_only"],
        "recipes": len(st.recipes), "own": len(own), "recipes_new": len(st.new_recipes),
        "revisions": len(st.revisions), "revisions_new": len(st.new_revisions),
        "by_model": dict(sorted(Counter(v["model"] for v in revs).items())),
        "estimated": sum(1 for v in revs if v.get("created_at_estimated")),
        "kinds_new": dict(Counter(st.revisions[i]["kind"] for i in st.new_revisions)),
        "rows_changed": len(b.changed), "linked": linked, "repinned": repinned,
        "book_copies_linked": book_linked, "visible": len(accepted_pins | own),
        "dangling_parents": sum(1 for r in rows if r.parent_id and r.parent_id not in by_row),
    }


# --- запись ---


def write(session: Session, b: Build) -> None:
    """Новые рецепты/версии (INSERT OR IGNORE) + новые списки блюд изменившихся версий
    (dishes_version + 1 — CAS у параллельного писателя увидит запись)."""
    st = b.store
    if st.new_recipes:
        session.exec(sqlite_insert(Recipe.__table__).on_conflict_do_nothing(),
                     params=[st.recipes[i] for i in st.new_recipes])
    if st.new_revisions:
        session.exec(sqlite_insert(RecipeRevision.__table__).on_conflict_do_nothing(),
                     params=[st.revisions[i] for i in st.new_revisions])
    for pid in b.changed:
        session.exec(
            update(PlanRow).where(PlanRow.id == pid)
            .values(dishes=b.pinned[pid],
                    dishes_version=func.coalesce(PlanRow.dishes_version, 0) + 1)
            .execution_options(synchronize_session=False)
        )


def ensure_ref_columns(session: Session) -> list[str]:
    """Nullable-колонки ссылок на рецепт (ratingrow/favoriterecipe/messagerow)."""
    added = []
    for table_name, cols in RECIPE_REF_COLUMNS.items():
        have = columns(session, table_name)
        for col in cols:
            if col not in have:
                session.exec(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" VARCHAR'))
                added.append(f"{table_name}.{col}")
    return added


def _latest_by_conv(rows: list[Row]) -> dict[str, Row]:
    out: dict[str, Row] = {}
    for r in sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True):
        out.setdefault(r.conversation_id, r)
    return out


def _dish_in(pinned: dict[str, list[dict]], plan_id: str | None, dish_id: str | None) -> dict | None:
    if not plan_id or not dish_id:
        return None
    return next((d for d in pinned.get(plan_id, []) if d.get("id") == dish_id), None)


def _naive(v) -> datetime | None:
    """created_at из SQLite (строка через text() или datetime) → naive UTC."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    return rs._naive_utc(v) if isinstance(v, datetime) else None


def rev_times_of(revisions) -> dict[str, tuple[datetime | None, bool]]:
    """id версии → (created_at, дата приблизительная) — из строк store (dict) или RecipeRevision."""
    out = {}
    for rid, v in revisions.items():
        if isinstance(v, dict):
            out[rid] = (_naive(v.get("created_at")), bool(v.get("created_at_estimated")))
        else:
            out[rid] = (_naive(v.created_at), bool(v.created_at_estimated))
    return out


def resolve_refs(session: Session, rows: list[Row], pinned: dict[str, list[dict]],
                 rev_times: dict[str, tuple[datetime | None, bool]], *,
                 only_missing: bool = False) -> dict:
    """Куда указывают оценки/избранное/реплики по правилам дизайна (без записи):
    👍/👎 рецепта — версия, закреплённая за моделью голоса в ТОЙ версии плана
    (plan_id, dish_id|target_id, model); ★ — (plan_id, dish_id); реплика обсуждения —
    (беседа, dish_id) в самой свежей версии плана беседы.

    Закрепление — то, что в плане СЕЙЧАС. Если эта версия сгенерирована позже голоса (точная
    дата, не «≈»), оценённый текст «↻» перезаписал на месте и его больше нет нигде: оценка
    получает только рецепт (revision_id = NULL — «неизвестно», а не чужой текст; угадывать
    более раннюю версию нельзя — она могла жить в другой версии плана). Голос можно сменить
    в течение 30 минут, а created_at — первый голос: смена после «↻» тоже уходит в NULL.

    only_missing — только строки с пустыми ссылками (дозаполнение: записанное приложением в
    момент голоса — точнее, его не пересчитываем и о нём не предупреждаем)."""
    latest = _latest_by_conv(rows)
    out: dict = {"ratings": {}, "favorites": {}, "messages": {}, "unresolved": [],
                 "overwritten": []}
    miss = " AND recipe_id IS NULL AND revision_id IS NULL" if only_missing else ""
    for rid_, plan_id, dish_id, target_id, model, voted in session.exec(text(
        "SELECT id, plan_id, dish_id, target_id, model, created_at FROM ratingrow "
        f"WHERE target_type = 'recipe'{miss}"
    )).all():
        d = _dish_in(pinned, plan_id, dish_id or target_id)
        rev = (d.get("rev_ids") or {}).get(model or "") if d else None
        if not (d and d.get("recipe_id") and rev):
            out["unresolved"].append(f"ratingrow:{rid_}")
            continue
        made, estimated = rev_times.get(rev, (None, True))
        voted_at = _naive(voted)
        if made and not estimated and voted_at and made > voted_at:
            out["overwritten"].append(f"ratingrow:{rid_}")
            rev = None
        out["ratings"][rid_] = (d["recipe_id"], rev)
    miss = " WHERE recipe_id IS NULL" if only_missing else ""
    for key, plan_id, dish_id in session.exec(text(
        f"SELECT key, plan_id, dish_id FROM favoriterecipe{miss}"
    )).all():
        d = _dish_in(pinned, plan_id, dish_id)
        if d and d.get("recipe_id"):
            out["favorites"][key] = d["recipe_id"]
        else:
            out["unresolved"].append(f"favoriterecipe:{key}")
    miss = " AND recipe_id IS NULL" if only_missing else ""
    for mid, conv, dish_id in session.exec(text(
        "SELECT id, conversation_id, dish_id FROM messagerow "
        f"WHERE discuss_target = 'recipe' AND dish_id IS NOT NULL{miss}"
    )).all():
        row = latest.get(conv)
        d = _dish_in(pinned, row.id if row else None, dish_id)
        if d and d.get("recipe_id"):
            out["messages"][mid] = d["recipe_id"]
        else:
            out["unresolved"].append(f"messagerow:{mid}")
    return out


def backfill_refs(session: Session, rows: list[Row], b: Build) -> dict:
    """Заполнить ПУСТЫЕ ссылки (записанное приложением в момент голоса — точнее, не трогаем).
    Оценка, чей текст перезаписан «↻», получает только recipe_id: следующий sync её уже не
    пересчитывает (ссылка не пустая) — NULL в revision_id остаётся честным «неизвестно»."""
    refs = resolve_refs(session, rows, b.pinned, rev_times_of(b.store.revisions),
                        only_missing=True)
    filled = Counter()
    for rid_, (rec, rev) in refs["ratings"].items():
        res = session.exec(text(
            "UPDATE ratingrow SET recipe_id = :r, revision_id = :v "
            "WHERE id = :id AND recipe_id IS NULL AND revision_id IS NULL"
        ), params={"r": rec, "v": rev, "id": rid_})
        filled["ratings"] += res.rowcount or 0
    for key, rec in refs["favorites"].items():
        res = session.exec(text(
            "UPDATE favoriterecipe SET recipe_id = :r WHERE key = :k AND recipe_id IS NULL"
        ), params={"r": rec, "k": key})
        filled["favorites"] += res.rowcount or 0
    for mid, rec in refs["messages"].items():
        res = session.exec(text(
            "UPDATE messagerow SET recipe_id = :r WHERE id = :id AND recipe_id IS NULL"
        ), params={"r": rec, "id": mid})
        filled["messages"] += res.rowcount or 0
    for u in refs["unresolved"]:
        logger.warning("recipes_v1: ссылка не разрешилась (остаётся NULL): %s", u)
    for u in refs["overwritten"]:
        logger.warning("recipes_v1: %s — оценённый текст перезаписан «↻» после голоса, "
                       "revision_id = NULL (только рецепт)", u)
    return {
        "ratings": len(refs["ratings"]), "overwritten": len(refs["overwritten"]),
        "favorites": len(refs["favorites"]), "messages": len(refs["messages"]),
        "unresolved": len(refs["unresolved"]), "filled": dict(filled),
    }


_HEADER = ("emoji", "desc", "servings", "prep_min", "cook_min", "tags", "storage")


def refresh_headers(session: Session, rows: list[Row], b: Build) -> int:
    """Снимок шапки рецепта — по блюду-ссылке Книги: свой рецепт — «Мои рецепты», рецепт
    плана — самый свежий принятый план с ним. Без ссылки — как при создании. source своего
    рецепта и name_key (идентичность) не меняем."""
    refs: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (r.library, r.decided_at or r.created_at), reverse=True):
        if r.status not in ("accepted", _LIBRARY):
            continue
        for d in b.pinned[r.id]:
            if d.get("recipe_id"):
                refs.setdefault(d["recipe_id"], d)
    n = 0
    now = rs._now()
    for rid, d in refs.items():
        cur = b.store.recipes.get(rid)
        if cur is None:
            continue
        want = {k: v for k, v in rs.recipe_row(
            "", rid, d, rs.PlanCtx("", None, False, now), origin=cur["origin"],
            first_plan_id="", at=now,
        ).items() if k in _HEADER}
        if norm_name(str(d.get("name") or "")) == cur["name_key"]:
            want["name"] = str(d.get("name") or "")
        diff = {k: v for k, v in want.items() if cur.get(k) != v}
        if diff:
            session.exec(update(Recipe).where(Recipe.id == rid).values(**diff, updated_at=now)
                         .execution_options(synchronize_session=False))
            cur.update(diff)
            n += 1
    return n


# --- сверка V1–V10 (только чтение; в apply — до COMMIT, на старте приложения — без записи) ---

# Строки, которые миграция и sync не удаляют и не меняют (design .final.migration шаг 13):
# таблица → первичный ключ. Колонки ссылок на рецепт (их пишет backfill) и блюда плана (их V2
# сверяет по-блюдно; dishes_version растёт у переписанных строк) — не в отпечатке.
_KEPT = {"conversation": "id", "planrow": "id", "messagerow": "id", "ratingrow": "id",
         "favoriterecipe": "key"}
_KEPT_SKIP = {**{t: set(cols) for t, cols in RECIPE_REF_COLUMNS.items()},
              "planrow": {"dishes", "dishes_version"}}


def kept_rows(session: Session) -> dict[str, dict[str, str]]:
    """Отпечатки строк бесед, планов (без блюд), сообщений, оценок, избранного: таблица →
    {pk: sha1 строки}. Снимок до записи против снимка после — V2 (ничего не удалено и не
    изменено мимо ссылок)."""
    out: dict[str, dict[str, str]] = {}
    for t, pk in _KEPT.items():
        if not table_exists(session, t):
            continue
        cols = sorted(columns(session, t) - _KEPT_SKIP.get(t, set()))
        sql = "SELECT " + ", ".join(f'"{c}"' for c in cols) + f' FROM "{t}"'
        rows: dict[str, str] = {}
        for r in session.exec(text(sql)).all():
            m = dict(zip(cols, r))
            rows[str(m[pk])] = hashlib.sha1(json.dumps(
                m, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()
        out[t] = rows
    return out


def _check(diffs: list[str], expected=None, actual=None, ok: bool | None = None) -> dict:
    return {
        "ok": (not diffs) if ok is None else ok, "expected": expected, "actual": actual,
        "diffs": diffs[:20], "n_diffs": len(diffs),
    }


def _strip_body(d: dict) -> dict:
    """Блюдо без тела рецепта (как будет после «похудения» JSON в фазе 3)."""
    out = {k: v for k, v in d.items() if k not in rs.BODY_KEYS}
    if isinstance(d.get("storage"), dict):
        out["storage"] = {k: v for k, v in d["storage"].items() if k != "note"}
    return out


def _legacy_form(d: dict) -> dict:
    """Ожидаемый вид блюда при чтении из таблиц: блюдо с variants — как есть; legacy-деталь
    (только плоские поля) — нормализованная dish_variants + apply_variant."""
    if d.get("variants"):
        return d
    variants = rs.variants_for(d)
    return apply_variant(d, next(iter(variants)), variants)


_VARIANT_FIELDS = (("ingredients", []), ("steps", []), ("tips", []), ("note", ""),
                   ("provider", ""), ("generated_at", ""))


def _norm(d: dict) -> dict:
    """Для сравнения: без закреплений и метаданных вариантов (они — в колонках версии),
    отсутствующий ключ = пустое значение."""
    out = {k: v for k, v in d.items() if k not in rs.PIN_KEYS}
    out["variants"] = {
        m: {k: v.get(k) or dflt for k, dflt in _VARIANT_FIELDS}
        for m, v in (d.get("variants") or {}).items()
    }
    for k in ("ingredients", "steps", "tips"):
        out[k] = d.get(k) or []
    for k in ("detail_provider", "detail_generated_at", "source", "active_model"):
        out[k] = d.get(k) or ""
    storage = dict(d.get("storage") or {})
    storage["note"] = storage.get("note") or ""
    out["storage"] = storage
    return out


def _dict_diff(a: dict, b: dict) -> list[str]:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def verify_session(
    session: Session,
    *,
    originals: dict[str, list[dict]] | None = None,
    raw_before: dict[str, str | None] | None = None,
    changed: list[str] | None = None,
    expected: dict | None = None,
    expected_refs: dict | None = None,
    strict_refs: bool = True,
    kept_before: dict[str, dict[str, str]] | None = None,
) -> dict:
    """Сверка таблиц с JSON. Ничего не пишет. originals/raw_before — блюда и сырой JSON до
    записи (V2: только добавились закрепления; непереписанные строки — тот же текст JSON),
    kept_before — отпечатки прочих строк до записи (V2: ни одна не удалена/не изменена),
    expected — счётчики сборки (V1), strict_refs — ссылки обязаны совпасть с текущими
    закреплениями (сразу после заполнения; в живой базе голос мог быть до «↻»)."""
    session.expire_all()
    rows = load_rows(session)
    by_id = {r.id: r for r in rows}
    revs = {r.id: r for r in session.exec(select(RecipeRevision)).all()}
    recipes = {r.id: r for r in session.exec(select(Recipe)).all()}
    checks: dict[str, dict] = {}

    # V1 — счётчики: каждое блюдо с рецептом закреплено.
    cats, diffs = Counter(), []
    for r in rows:
        for d in r.dishes:
            variants = rs.variants_for(d)
            if variants:
                cats["with_recipe"] += 1
                if d.get("recipe_id") and list((d.get("rev_ids") or {}).keys()) == list(variants):
                    cats["pinned"] += 1
                else:
                    diffs.append(f"{r.id}/{d.get('id')}: не закреплено")
            elif d.get("ingredients") or d.get("steps"):
                cats["plan_owned"] += 1
            else:
                cats["header_only"] += 1
    actual = {
        "planrows": len(rows), "dishes": sum(len(r.dishes) for r in rows), **cats,
        "recipes": len(recipes), "own": sum(1 for x in recipes.values() if x.origin == "own"),
        "revisions": len(revs),
        "by_model": dict(sorted(Counter(v.model for v in revs.values()).items())),
        "estimated": sum(1 for v in revs.values() if v.created_at_estimated),
    }
    exp = None
    if expected:
        exp = {k: expected[k] for k in ("planrows", "dishes", "pinned", "plan_owned",
                                        "header_only", "recipes", "own", "revisions",
                                        "by_model", "estimated") if k in expected}
        diffs += [f"{k}: ждали {v}, в базе {actual.get(k, 0)}" for k, v in exp.items()
                  if actual.get(k, 0) != v]
    checks["V1_counts"] = _check(diffs, exp, actual)

    # V2 — только добавились закрепления; непереписанные строки — тот же текст JSON; строки
    # бесед/планов/сообщений/оценок/избранного не удалены и не изменены (кроме ссылок).
    diffs = []
    if originals is not None:
        for r in rows:
            orig = originals.get(r.id)
            if orig is None:
                diffs.append(f"{r.id}: новая строка")
                continue
            if len(orig) != len(r.dishes):
                diffs.append(f"{r.id}: блюд было {len(orig)}, стало {len(r.dishes)}")
                continue
            for o, d in zip(orig, r.dishes):
                a, b = rs.strip_pins(o), rs.strip_pins(d)
                if a != b:
                    diffs.append(f"{r.id}/{o.get('id')}: {_dict_diff(a, b)}")
        if raw_before is not None:
            moved = set(changed or [])
            diffs += [f"{r.id}: JSON переписан" for r in rows
                      if r.id not in moved and r.raw != raw_before.get(r.id)]
    if kept_before is not None:
        now = kept_rows(session)
        for t, before in kept_before.items():
            after = now.get(t, {})
            diffs += [f"{t}:{k}: строка удалена" for k in sorted(before.keys() - after.keys())]
            diffs += [f"{t}:{k}: новая строка" for k in sorted(after.keys() - before.keys())]
            diffs += [f"{t}:{k}: строка изменилась" for k in sorted(before.keys() & after.keys())
                      if before[k] != after[k]]
    checks["V2_additive"] = _check(
        diffs, ok=None if originals is not None or kept_before is not None else True,
        actual="нет снимка — пропуск" if originals is None and kept_before is None else None)

    # V3 — каждый вариант (блюдо, модель) закреплён на версию с тем же рецептом/текстом/мета.
    diffs = []
    for r in rows:
        for d in r.dishes:
            pins = d.get("rev_ids") or {}
            for m, v in rs.variants_for(d).items():
                rev = revs.get(pins.get(m, ""))
                where = f"{r.id}/{d.get('id')}/{m}"
                if rev is None:
                    diffs.append(f"{where}: нет версии")
                    continue
                want_meta = rs.meta_hash(str(v.get("provider") or ""),
                                         str(v.get("generated_at") or ""),
                                         str(d.get("source") or ""))
                bad = [n for n, ok in (
                    ("recipe", rev.recipe_id == d.get("recipe_id")), ("model", rev.model == m),
                    ("content", rev.content_hash == rs.content_hash(v)),
                    ("meta", rev.meta_hash == want_meta),
                ) if not ok]
                if bad:
                    diffs.append(f"{where}: {bad}")
    checks["V3_pins"] = _check(diffs)

    # V4 — чтение из таблиц даёт то же блюдо (имитация фазы 3: тело снято, восстановлено) и
    # тот же WeekPlan API; у legacy-деталей разрешены только activeModel/variantModels.
    diffs, hydrated = [], {}
    for r in rows:
        hyd = []
        for d in r.dishes:
            if d.get("rev_ids"):
                h = rs.hydrate_dish(_strip_body(d), revs)
                if _norm(h) != _norm(_legacy_form(d)):
                    diffs.append(f"{r.id}/{d.get('id')}: {_dict_diff(_norm(h), _norm(_legacy_form(d)))}")
                hyd.append(h)
            else:
                hyd.append(d)
        hydrated[r.id] = hyd
        try:
            wj = to_week_plan(r.ns()).model_dump()
            wh = to_week_plan(r.ns(hyd)).model_dump()
        except Exception as exc:  # noqa: BLE001 — битое блюдо в старых данных: в отчёт
            diffs.append(f"{r.id}: WeekPlan не строится: {str(exc)[:120]}")
            continue
        legacy = [not d.get("variants") for d in r.dishes]
        for i, (a, b) in enumerate(zip(wj.pop("dishes"), wh.pop("dishes"))):
            if legacy[i]:
                for k in ("active_model", "variant_models"):
                    a.pop(k, None)
                    b.pop(k, None)
            if a != b:
                diffs.append(f"{r.id}/dish#{i}: API {_dict_diff(a, b)}")
        if wj != wh:
            diffs.append(f"{r.id}: API плана {_dict_diff(wj, wh)}")
    checks["V4_parity"] = _check(diffs, actual={"rows": len(rows)})

    # V5 — подписи покупок и плана готовки не меняются (иначе лишняя нормализация моделью).
    diffs, stored_ok, shop_cached, cook_cached, cook_ok = [], 0, 0, 0, 0
    for r in rows:
        sig_j = shopping_base(r.ns())[1]
        sig_h = shopping_base(r.ns(hydrated[r.id]))[1]
        if sig_j != sig_h:
            diffs.append(f"{r.id}: подпись покупок {sig_j[:8]} → {sig_h[:8]}")
        if r.shopping_sig:
            shop_cached += 1
            stored_ok += int(r.shopping_sig == sig_h)
        cj, ch = cook_sig(r.ns()), cook_sig(r.ns(hydrated[r.id]))
        if cj != ch:
            diffs.append(f"{r.id}: подпись готовки")
        if (r.cooking_plan or {}).get("sig"):
            cook_cached += 1
            cook_ok += int(r.cooking_plan["sig"] == ch)
    checks["V5_sigs"] = _check(diffs, actual={
        "shopping_cached": shop_cached, "shopping_cache_valid": stored_ok,
        "cooking_cached": cook_cached, "cooking_cache_valid": cook_ok,
    })

    pinned_now = {r.id: r.dishes for r in rows}
    refs_now = (resolve_refs(session, rows, pinned_now, rev_times_of(revs)) if strict_refs
                else None)

    # V6 — 👍/👎 рецепта → версия того текста, за который голосовали (или только рецепт, если
    # тот текст перезаписан «↻» после голоса — revision_id пуст).
    diffs, mapped = [], 0
    for rid_, model, rec, rev_id in session.exec(text(
        "SELECT id, model, recipe_id, revision_id FROM ratingrow WHERE target_type = 'recipe'"
    )).all():
        if not rec and not rev_id:
            continue
        mapped += 1
        rev = revs.get(rev_id) if rev_id else None
        if rev_id and (rev is None or rev.recipe_id != rec or rev.model != (model or "")):
            diffs.append(f"ratingrow:{rid_}: версия {rev_id} не та")
        elif not rev_id and rec not in recipes:
            diffs.append(f"ratingrow:{rid_}: нет рецепта {rec}")
        elif refs_now is not None and refs_now["ratings"].get(rid_) != (rec, rev_id or None):
            diffs.append(f"ratingrow:{rid_}: не совпадает с закреплением в плане")
    exp_n = len(refs_now["ratings"]) if refs_now else None
    if exp_n is not None and exp_n != mapped:
        diffs.append(f"оценок разрешается {exp_n}, записано {mapped}")
    checks["V6_ratings"] = _check(diffs, exp_n, mapped)

    # V7 — ★ → рецепт; V10 — реплики обсуждения рецепта → рецепт.
    for name, sql, kind in (
        ("V7_favorites", "SELECT key, recipe_id FROM favoriterecipe", "favorites"),
        ("V10_messages", "SELECT id, recipe_id FROM messagerow WHERE discuss_target = 'recipe' "
                         "AND dish_id IS NOT NULL", "messages"),
    ):
        diffs, mapped = [], 0
        for key, rec in session.exec(text(sql)).all():
            if not rec:
                continue
            mapped += 1
            if rec not in recipes:
                diffs.append(f"{kind}:{key}: нет рецепта {rec}")
            elif refs_now is not None and refs_now[kind].get(key) != rec:
                diffs.append(f"{kind}:{key}: не совпадает с закреплением")
        exp_n = len(refs_now[kind]) if refs_now else None
        if exp_n is not None and exp_n != mapped:
            diffs.append(f"разрешается {exp_n}, записано {mapped}")
        checks[name] = _check(diffs, exp_n, mapped)

    # V8 — Книга и список «Рецепты» по таблицам = по JSON (порядок, названия для промпта).
    diffs = []
    idx_j = book_index(session)
    names_j = book_names(session, index=idx_j)
    book = rs.book_hydrated(session)
    entries = rs.book_entries(session, book)
    names_t = book_names(session, index=rs.book_index_tables(session, book))
    if names_j != names_t:
        diffs.append(f"названия Книги: JSON {names_j[:5]}… ≠ таблицы {names_t[:5]}…")
    list_j, seen, unpinned, dups = [], set(), 0, 0
    for row in book_rows(session):
        for d in row.dishes or []:
            if not str(d.get("name") or "").strip() or not d.get("id"):
                continue
            rid = d.get("recipe_id")
            if not rid:
                unpinned += 1
            elif rid in seen:
                dups += 1
            else:
                seen.add(rid)
                list_j.append((row.id, d["id"]))
    list_t = [(e.plan_id, e.dish_id) for e in entries]
    if list_j != list_t:
        diffs.append(f"список: JSON {len(list_j)} ≠ таблицы {len(list_t)}")
    checks["V8_book"] = _check(diffs, actual={
        "visible": len(entries), "book_keys": len(idx_j), "list_rows": len(list_j) + unpinned
        + dups, "rows_without_recipe": unpinned, "same_recipe_rows": dups,
    })

    # V9 — целостность: сироты, дубли, чужие закрепления, детерминированные id и хэши.
    # servings версии — справочно (на сколько порций писали; в id и хэши не входит, сверять
    # не с чем: шапка блюда могла поменяться после генерации).
    diffs = []
    for (rev_id,) in session.exec(text(
        "SELECT v.id FROM recipe_revision v LEFT JOIN recipe r ON r.id = v.recipe_id "
        "WHERE r.id IS NULL"
    )).all():
        diffs.append(f"версия-сирота {rev_id}")
    for dup in session.exec(text(
        "SELECT recipe_id, model, content_hash, meta_hash, count(*) FROM recipe_revision "
        "GROUP BY 1, 2, 3, 4 HAVING count(*) > 1"
    )).all():
        diffs.append(f"дубль версии {tuple(dup)}")
    for r in rows:
        for d in r.dishes:
            for m, rev_id in (d.get("rev_ids") or {}).items():
                rev = revs.get(rev_id)
                if rev is None or rev.recipe_id != d.get("recipe_id") or rev.model != m:
                    diffs.append(f"{r.id}/{d.get('id')}/{m}: закрепление на чужую/нет версии")
            if d.get("recipe_id") and d["recipe_id"] not in recipes:
                diffs.append(f"{r.id}/{d.get('id')}: нет рецепта {d['recipe_id']}")
    for rec in recipes.values():
        if not rec.key.startswith("pin:") and rec.id != rs.recipe_id_for(rec.key):
            diffs.append(f"рецепт {rec.id}: id не от ключа")
    for v in revs.values():
        body = {"ingredients": v.ingredients, "steps": v.steps, "tips": v.tips, "note": v.note}
        if (v.content_hash != rs.content_hash(body)
                or v.meta_hash != rs.meta_hash(v.provider, v.generated_at, v.source_used)
                or v.id != rs.revision_id_for(v.recipe_id, v.model, v.content_hash,
                                              v.meta_hash)):
            diffs.append(f"версия {v.id}: хэш/id не сходятся с текстом")
    pinned_recipes = {d.get("recipe_id") for r in rows for d in r.dishes}
    trig = session.exec(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'recipe_revision_immutable'"
    )).first()
    if not trig:
        diffs.append("нет триггера recipe_revision_immutable")
    quick = session.exec(text("PRAGMA quick_check")).first()[0]
    if quick != "ok":
        diffs.append(f"quick_check: {quick}")
    checks["V9_integrity"] = _check(diffs, actual={
        "recipes_without_pins": sum(1 for rid in recipes if rid not in pinned_recipes),
        "dangling_parents": sum(1 for r in rows if r.parent_id and r.parent_id not in by_id),
    })

    failed = [k for k, c in checks.items() if not c["ok"]]
    return {"ok": not failed, "failed": failed, "checks": checks,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# --- шаги: apply / sync / strip / drop / status (транзакцию открывает и коммитит вызывающий) ---


def _snapshot(rows: list[Row]) -> tuple[dict[str, list[dict]], dict[str, str | None]]:
    return {r.id: copy.deepcopy(r.dishes) for r in rows}, {r.id: r.raw for r in rows}


def apply_step(session: Session, *, backup_path: str = "", commit: str = "",
               hook=None) -> dict:
    """Создать таблицы/колонки/триггер, перенести рецепты, заполнить ссылки, сверить и
    поставить маркер. Вызывающий коммитит; при исключении — откатывает (база прежняя).
    hook(stage) — для тестов: сбой посреди транзакции."""
    t0 = time.monotonic()
    RECIPE_METADATA.create_all(session.connection())
    added = ensure_ref_columns(session)
    session.exec(text(TRIGGER_SQL))
    rows = load_rows(session)
    originals, raw_before = _snapshot(rows)
    kept = kept_rows(session)
    b = build(rows, load_store(session), default_kind="migrated")
    write(session, b)
    if hook:
        hook("after_write")
    refs = backfill_refs(session, rows, b)
    headers = refresh_headers(session, rows, b)
    report = verify_session(session, originals=originals, raw_before=raw_before,
                            changed=b.changed, expected=b.stats, strict_refs=True,
                            kept_before=kept)
    if hook:
        hook("after_verify")
    if not report["ok"]:
        raise VerifyFailed(report)
    stats = {**b.stats, "refs": refs, "headers_refreshed": headers, "columns_added": added,
             "seconds": round(time.monotonic() - t0, 2)}
    session.exec(sqlite_insert(SchemaMigration.__table__).values(
        name=STEP, applied_at=rs._now(), app_commit=commit, backup_path=backup_path,
        stats=stats, verify=report, verified_at=rs._now(),
    ))
    logger.info("recipes_v1 применён: %d рецептов (%d своих), %d версий, %d блюд закреплено, "
                "ссылки %d/%d/%d", stats["recipes"], stats["own"], stats["revisions"],
                stats["pinned"], refs["ratings"], refs["favorites"], refs["messages"])
    return {"stats": stats, "verify": report}


def sync_session(session: Session, *, hook=None) -> dict:
    """Догнать JSON: текст без версии (старый код после отката, двойная запись со сбоем) →
    версии kind=resync, перезакрепить устаревшие закрепления, закрепить новые блюда,
    дозаполнить пустые ссылки, обновить снимки шапок. В норме — всё по нулям."""
    if marker(session) is None:
        raise MigrationError("recipes_v1 не применён — sync нечего догонять")
    t0 = time.monotonic()
    rows = load_rows(session)
    originals, raw_before = _snapshot(rows)
    kept = kept_rows(session)
    b = build(rows, load_store(session), default_kind="resync")
    write(session, b)
    if hook:
        hook("after_write")
    refs = backfill_refs(session, rows, b)
    headers = refresh_headers(session, rows, b)
    report = verify_session(session, originals=originals, raw_before=raw_before,
                            changed=b.changed, strict_refs=False, kept_before=kept)
    if not report["ok"]:
        raise VerifyFailed(report)
    st = b.stats
    result = {
        "revisions_new": st["revisions_new"], "recipes_new": st["recipes_new"],
        "rows_changed": st["rows_changed"], "linked": st["linked"], "repinned": st["repinned"],
        "kinds_new": st["kinds_new"], "refs_filled": refs["filled"],
        "headers_refreshed": headers, "seconds": round(time.monotonic() - t0, 2),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    m = marker(session)
    session.exec(
        update(SchemaMigration).where(SchemaMigration.name == STEP)
        .values(stats={**(m["stats"] or {}), "last_sync": result}, verify=report,
                verified_at=rs._now())
        .execution_options(synchronize_session=False)
    )
    logger.info("recipes sync: +%d revisions, %d re-pins, %d linked, +%d recipes",
                st["revisions_new"], st["repinned"], st["linked"], st["recipes_new"])
    return {**result, "verify": report}


def strip_session(session: Session) -> dict:
    """L3, шаг 1: убрать recipe_id/rev_ids из всех блюд (и маркер — двойная запись выключится).
    Отказ, если у закреплённого блюда нет своего тела в JSON (после «похудения» фазы 3)."""
    rows = load_rows(session)
    changed = 0
    for r in rows:
        new = []
        for d in r.dishes:
            if d.get("rev_ids") and not rs.variants_for(d):
                raise MigrationError(f"{r.id}/{d.get('id')}: в JSON нет тела рецепта — "
                                     f"сначала export-json (фаза 3)")
            new.append(rs.strip_pins(d))
        if new != r.dishes:
            session.exec(
                update(PlanRow).where(PlanRow.id == r.id)
                .values(dishes=new, dishes_version=func.coalesce(PlanRow.dishes_version, 0) + 1)
                .execution_options(synchronize_session=False)
            )
            changed += 1
    removed = 0
    if table_exists(session, "schema_migration"):
        removed = session.exec(text(
            "DELETE FROM schema_migration WHERE name = :n"), params={"n": STEP}).rowcount
    after = load_rows(session)
    left = [f"{r.id}/{d.get('id')}" for r in after for d in r.dishes
            if any(k in d for k in rs.PIN_KEYS)]
    if left:
        raise MigrationError(f"strip: закрепления остались: {left[:5]}")
    before = {r.id: [rs.strip_pins(d) for d in r.dishes] for r in rows}
    if any(before[r.id] != r.dishes for r in after):
        raise MigrationError("strip: блюда изменились не только закреплениями")
    return {"rows_changed": changed, "marker_removed": bool(removed)}


def drop_session(session: Session) -> dict:
    """L3, шаг 2: снять таблицы рецептов и маркер, обнулить ссылки. Планы не трогаем — если
    закрепления в JSON остались (strip не делали), повторный apply восстановит те же id."""
    session.exec(text("DROP TRIGGER IF EXISTS recipe_revision_immutable"))
    session.exec(text("DROP TABLE IF EXISTS recipe_revision"))
    session.exec(text("DROP TABLE IF EXISTS recipe"))
    if table_exists(session, "schema_migration"):
        session.exec(text("DELETE FROM schema_migration WHERE name LIKE 'recipes_v1%'"))
        if session.exec(text("SELECT count(*) FROM schema_migration")).first()[0] == 0:
            session.exec(text("DROP TABLE schema_migration"))
    nulled = []
    for table_name, cols in RECIPE_REF_COLUMNS.items():
        have = columns(session, table_name)
        for col in cols:
            if col in have:
                session.exec(text(f'UPDATE "{table_name}" SET "{col}" = NULL'))
                nulled.append(f"{table_name}.{col}")
    pins_left = sum(1 for r in load_rows(session) for d in r.dishes if d.get("rev_ids"))
    return {"columns_nulled": nulled, "dishes_with_pins_left": pins_left}


def status_session(session: Session) -> dict:
    m = marker(session)
    out: dict = {
        "marker": None, "tables": {t: table_exists(session, t) for t in RECIPE_METADATA.tables},
        "ref_columns": {t: sorted(set(cols) & columns(session, t))
                        for t, cols in RECIPE_REF_COLUMNS.items()},
    }
    if m:
        verify = m.get("verify") or {}
        out["marker"] = {
            "applied_at": str(m["applied_at"]), "app_commit": m["app_commit"],
            "backup_path": m["backup_path"], "verified_at": str(m["verified_at"]),
            "verify_ok": verify.get("ok"), "verify_failed": verify.get("failed"),
            "last_sync": (m.get("stats") or {}).get("last_sync"),
        }
    if out["tables"]["recipe"] and out["tables"]["recipe_revision"]:
        out["recipes"] = session.exec(text("SELECT count(*) FROM recipe")).first()[0]
        out["revisions"] = session.exec(text("SELECT count(*) FROM recipe_revision")).first()[0]
    rows = load_rows(session)
    out["planrows"] = len(rows)
    out["dishes_pinned"] = sum(1 for r in rows for d in r.dishes if d.get("rev_ids"))
    out["dishes_with_recipe"] = sum(1 for r in rows for d in r.dishes if rs.variants_for(d))
    return out


def pin_map(session: Session) -> dict[tuple[str, str], tuple]:
    """(план, блюдо) → (recipe_id, закрепления) — для сравнения прогонов в репетиции."""
    return {
        (r.id, str(d.get("id"))): (d.get("recipe_id"), tuple((d.get("rev_ids") or {}).items()))
        for r in load_rows(session) for d in r.dishes if d.get("rev_ids")
    }


def id_sets(session: Session) -> dict[str, set[str]]:
    return {
        "recipes": set(session.exec(select(Recipe.id)).all()),
        "revisions": set(session.exec(select(RecipeRevision.id)).all()),
    }
