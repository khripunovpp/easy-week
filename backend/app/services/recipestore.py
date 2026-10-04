"""Хранилище рецептов: таблицы recipe / recipe_revision рядом с JSON блюд плана (фаза 1).

Зачем: в JSON плана рецепт живёт копиями (каждая версия плана, каждый план из Книги), а «↻»
перезаписывает вариант модели — старый текст пропадает. Здесь у рецепта одна строка
(recipe), а каждый сгенерированный текст — неизменная версия (recipe_revision).

Фаза 1 — «двойная запись, чтение по-старому»:
- таблицы создаёт и наполняет только CLI миграции (app/migrations; deploy/update.sh при
  остановленном сервисе), приложение на старте схему не меняет;
- пока миграция применена (маркер recipes_v1 в schema_migration), каждая запись блюд плана
  (services/planstore) закрепляет блюда за версиями — в той же транзакции, в SAVEPOINT. Сбой
  здесь запись JSON не роняет: ERROR в лог, метрика, следующий деплой лечит `migrations sync`;
- приложение ЧИТАЕТ только JSON; hydrate_* и book_entries — для сверки (verify) и фазы 2.

Блюдо плана (dict в planrow.dishes) получает ровно два ключа, остальное не трогаем:
- recipe_id — рецепт блюда (правила поиска — recipe_for);
- rev_ids — {ключ модели: id версии} в порядке вариантов; какой показан — по-прежнему
  active_model.

Версия — чистая функция содержимого: id = uuid5(рецепт|модель|content_hash|meta_hash), поэтому
закрепление пересчитывается из JSON при каждой записи (лишних версий не плодит), а снять и
заново применить миграцию — те же id.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge
from sqlalchemy import column, table, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from ..models import RECIPE_REF_COLUMNS, PlanRow, Recipe, RecipeRevision
from .history import norm_name
from .variants import apply_variant, dish_variants

logger = logging.getLogger("easy_week.recipes")

MARKER = "recipes_v1"
# Ключи закрепления в блюде плана: всё остальное в блюде — прежний JSON, его не меняем.
PIN_KEYS = ("recipe_id", "rev_ids")
# Тело рецепта в блюде (зеркало активного варианта + варианты) — то, что даёт hydrate.
BODY_KEYS = ("variants", "ingredients", "steps", "tips", "detail_provider",
             "detail_generated_at", "source")

# Пространства имён uuid5 — менять нельзя: от них зависят id, уже записанные в JSON планов.
NS_RECIPE = uuid.uuid5(uuid.NAMESPACE_URL, "https://easy-week/recipe")
NS_REV = uuid.uuid5(uuid.NAMESPACE_URL, "https://easy-week/recipe_revision")

# Служебный план «Мои рецепты» (= recipebook.LIBRARY_ID/LIBRARY_STATUS; recipebook импортирует
# planstore → сюда, поэтому не импортируем его, а повторяем константу).
_LIBRARY = "library"

_sync_total = Counter(
    "easyweek_recipe_sync_total", "Двойная запись блюд плана в таблицы рецептов", ["result"]
)
_hydrate_miss = Counter(
    "easyweek_recipe_hydrate_miss_total", "Закрепление блюда на отсутствующую версию рецепта"
)
_store_tables = Gauge(
    "easyweek_recipe_store_tables", "Рецепты читаются из таблиц (1) или из JSON планов (0)"
)
_store_tables.set(0)  # фаза 1: чтение — только JSON


# --- содержимое и id ---


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def canonical(v: dict) -> str:
    """Каноничный JSON текста рецепта: то, что видит пользователь (не метаданные)."""
    return json.dumps(
        [v.get("ingredients") or [], v.get("steps") or [], v.get("tips") or [],
         v.get("note") or ""],
        sort_keys=True, ensure_ascii=False,
    )


def content_hash(v: dict) -> str:
    return _sha1(canonical(v))


def meta_hash(provider: str, generated_at: str, source_used: str) -> str:
    """Правило ничьей: тот же текст с другим провайдером, датой или текстом своего рецепта —
    отдельная версия (иначе одно значение молча победило бы другое)."""
    return _sha1(f"{provider}|{generated_at}|{source_used}")


def recipe_id_for(key: str) -> str:
    return uuid.uuid5(NS_RECIPE, key).hex


def revision_id_for(recipe_id: str, model: str, chash: str, mhash: str) -> str:
    return uuid.uuid5(NS_REV, f"{recipe_id}|{model}|{chash}|{mhash}").hex


def pin_id(recipe_id: str, model: str, v: dict, source: str) -> str:
    """id версии, на которую ДОЛЖНО указывать закрепление варианта v (модель model) блюда с
    рецептом recipe_id и текстом своего рецепта source: чистая функция содержимого."""
    meta = meta_hash(str(v.get("provider") or ""), str(v.get("generated_at") or ""), source)
    return revision_id_for(recipe_id, model, content_hash(v), meta)


def lineage_key(first_plan_id: str, dish_id: str, name_key: str) -> str:
    return f"lin:{first_plan_id}/{dish_id}/{name_key}"


def own_key(dish_id: str) -> str:
    return f"own:{dish_id}"


def variants_for(dish: dict) -> dict[str, dict]:
    """Варианты блюда так, как их хранят таблицы: services.variants.dish_variants, но у
    legacy-детали (плоские поля без variants) generated_at = detail_generated_at — иначе дата
    терялась бы при чтении из таблиц. Пусто — блюдо без рецепта (или спека Cloudflare без
    провайдера): такие остаются в JSON как есть."""
    variants = dish_variants(dish)
    if variants and not dish.get("variants"):
        (key, v), = variants.items()
        variants = {key: {**v, "generated_at": dish.get("detail_generated_at") or ""}}
    return variants


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def parse_generated_at(s: str) -> datetime | None:
    """generated_at варианта (ISO, обычно с +00:00) → naive UTC, как пишет SQLite; битое — None."""
    try:
        return _naive_utc(datetime.fromisoformat(s)) if s else None
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def strip_pins(dish: dict) -> dict:
    return {k: v for k, v in dish.items() if k not in PIN_KEYS}


def _same_dish(dishes: Iterable[dict], dish_id: str, name_key: str) -> dict | None:
    for d in dishes:
        if d.get("id") == dish_id and norm_name(str(d.get("name") or "")) == name_key:
            return d
    return None


# --- где лежат рецепты: в памяти (миграция) или в базе (двойная запись) ---


@dataclass(frozen=True)
class PlanCtx:
    """Версия плана, блюда которой закрепляем: id, беседа, «Мои рецепты» ли это и с какого
    времени она существует (дата для версий без generated_at)."""

    id: str
    conversation_id: str | None
    library: bool
    created_at: datetime


class _Store:
    """Что нужно правилам закрепления от хранилища. _MemStore — миграция (всё в памяти,
    потом одной транзакцией в базу), _DbStore — двойная запись в живом приложении."""

    def has_recipe(self, rid: str) -> bool: ...
    def recipes_named(self, name_key: str) -> list[str]: ...
    def revision_pairs(self, rid: str) -> set[tuple[str, str]]: ...
    def has_revision(self, rev_id: str, rid: str) -> bool: ...  # rid — рецепт этой версии
    def add_recipe(self, row: dict) -> None: ...
    def add_revision(self, row: dict) -> None: ...


@dataclass
class MemStore(_Store):
    """Рецепты и версии в памяти — для чистой сборки миграции (build) и её проверки."""

    recipes: dict[str, dict] = field(default_factory=dict)  # порядок = порядок создания
    revisions: dict[str, dict] = field(default_factory=dict)
    new_recipes: list[str] = field(default_factory=list)
    new_revisions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._named: dict[str, list[str]] = {}
        self._pairs: dict[str, set[tuple[str, str]]] = {}
        for rid, r in self.recipes.items():
            self._named.setdefault(r["name_key"], []).append(rid)
        for rev in self.revisions.values():
            self._pairs.setdefault(rev["recipe_id"], set()).add(
                (rev["model"], rev["content_hash"]))

    def has_recipe(self, rid: str) -> bool:
        return rid in self.recipes

    def recipes_named(self, name_key: str) -> list[str]:
        return list(self._named.get(name_key, []))

    def revision_pairs(self, rid: str) -> set[tuple[str, str]]:
        return self._pairs.get(rid, set())

    def has_revision(self, rev_id: str, rid: str) -> bool:
        return rev_id in self.revisions

    def add_recipe(self, row: dict) -> None:
        self.recipes[row["id"]] = row
        self.new_recipes.append(row["id"])
        self._named.setdefault(row["name_key"], []).append(row["id"])

    def add_revision(self, row: dict) -> None:
        self.revisions[row["id"]] = row
        self.new_revisions.append(row["id"])
        self._pairs.setdefault(row["recipe_id"], set()).add((row["model"], row["content_hash"]))


class _DbStore(_Store):
    """Рецепты и версии в базе (сессия вызывающего, внутри его SAVEPOINT). Вставка — INSERT OR
    IGNORE по детерминированному id: две версии плана, впервые открывшие одно блюдо
    одновременно, получают одну и ту же строку без IntegrityError.

    prefetch(рецепты) — двумя запросами узнать, какие из них есть, и id всех их версий: обычная
    запись плана, где блюда уже закреплены, дальше обходится без запроса на каждый вариант."""

    def __init__(self, session: Session) -> None:
        self.s = session
        self.inserted_recipes = 0
        self.inserted_revisions = 0
        self._known: set[str] = set()  # рецепты, о которых всё загружено prefetch
        self._recipes: set[str] = set()  # из них — существующие (+ вставленные нами)
        self._revs: set[str] = set()  # существующие версии (prefetch + вставленные нами)

    def prefetch(self, rids: Iterable[str]) -> None:
        want = sorted({str(r) for r in rids if r} - self._known)
        for i in range(0, len(want), 500):
            chunk = want[i:i + 500]
            self._recipes.update(self.s.exec(select(Recipe.id).where(Recipe.id.in_(chunk))).all())
            self._revs.update(self.s.exec(
                select(RecipeRevision.id).where(RecipeRevision.recipe_id.in_(chunk))).all())
            self._known.update(chunk)

    def has_recipe(self, rid: str) -> bool:
        if rid in self._known or rid in self._recipes:
            return rid in self._recipes
        return self.s.exec(select(Recipe.id).where(Recipe.id == rid)).first() is not None

    def recipes_named(self, name_key: str) -> list[str]:
        return list(self.s.exec(
            select(Recipe.id).where(Recipe.name_key == name_key)
            .order_by(Recipe.created_at, Recipe.key)
        ).all())

    def revision_pairs(self, rid: str) -> set[tuple[str, str]]:
        rows = self.s.exec(
            select(RecipeRevision.model, RecipeRevision.content_hash)
            .where(RecipeRevision.recipe_id == rid)
        ).all()
        return {(m, h) for m, h in rows}

    def has_revision(self, rev_id: str, rid: str) -> bool:
        if rev_id in self._revs or rid in self._known:
            return rev_id in self._revs
        return self.s.exec(
            select(RecipeRevision.id).where(RecipeRevision.id == rev_id)
        ).first() is not None

    def add_recipe(self, row: dict) -> None:
        res = self.s.exec(sqlite_insert(Recipe.__table__).values(**row).on_conflict_do_nothing())
        self.inserted_recipes += res.rowcount or 0
        self._recipes.add(row["id"])  # есть — наша вставка или чужая (тот же детерм. id)

    def add_revision(self, row: dict) -> None:
        res = self.s.exec(
            sqlite_insert(RecipeRevision.__table__).values(**row).on_conflict_do_nothing()
        )
        self.inserted_revisions += res.rowcount or 0
        self._revs.add(row["id"])


# --- правила: какой рецепт у блюда, какие версии закреплены ---


def recipe_row(key: str, rid: str, dish: dict, plan: PlanCtx, *, origin: str,
               first_plan_id: str, at: datetime) -> dict:
    """Строка recipe по блюду (шапка — снимок этого блюда)."""
    name = str(dish.get("name") or "")
    storage = dish.get("storage") or {}
    return {
        "id": rid, "key": key, "name": name, "name_key": norm_name(name),
        "emoji": str(dish.get("emoji") or "🍽️"), "desc": str(dish.get("desc") or ""),
        # Свой рецепт: текст пользователя как есть (в т.ч. уже дописанные «Уточнение: …»).
        "source": str(dish.get("source") or "") if origin == "own" else "",
        "origin": origin,
        "servings": _int(dish.get("servings"), 4), "prep_min": _int(dish.get("prep_min")),
        "cook_min": _int(dish.get("cook_min")),
        "tags": [str(t) for t in (dish.get("tags") or [])],
        "storage": {k: storage[k] for k in ("vacuum", "freeze", "shelf_life_days")
                    if k in storage},
        "conversation_id": plan.conversation_id,
        "origin_plan_id": first_plan_id, "origin_dish_id": str(dish.get("id") or ""),
        "created_at": at, "updated_at": at,
    }


def _book_match(store: _Store, variants: dict, name_key: str) -> str | None:
    """Блюдо из Книги (from_book) → рецепт-источник, только если ВСЁ его содержимое (каждая
    пара модель+текст) уже есть у одного рецепта с тем же названием. Так копия связывается со
    своим источником, а случайный тёзка с другим текстом — нет."""
    pairs = {(m, content_hash(v)) for m, v in variants.items()}
    for rid in store.recipes_named(name_key):
        if pairs <= store.revision_pairs(rid):
            return rid
    return None


# Предки версии плана (ближайший первым): (id версии, её блюда). Список или ленивый _DbChain.
Chain = Iterable[tuple[str, list[dict]]]


def _derive(store: _Store, plan: PlanCtx, dish: dict, name_key: str, variants: dict,
            chain: Chain, at: datetime) -> tuple[str, dict | None]:
    """Правила 2–4 (без готового dish.recipe_id): (id рецепта, строка recipe для вставки)."""
    dish_id = str(dish.get("id") or "")
    # 2. Ближайшая версия-предок с тем же блюдом (id + название) и рецептом — наследуем.
    for _, anc in chain:
        d = _same_dish(anc, dish_id, name_key)
        if d is not None and d.get("recipe_id") and store.has_recipe(str(d["recipe_id"])):
            return str(d["recipe_id"]), None
    # 3. Копия из Книги — к источнику, только при полном совпадении текста.
    if dish.get("from_book"):
        rid = _book_match(store, variants, name_key)
        if rid:
            return rid, None
    # 4. Свой рецепт — по id блюда в «Моих рецептах»; рецепт плана — по линии версий:
    # первое появление = самый старый существующий предок с тем же блюдом (id + название).
    if plan.library:
        key, origin, first = own_key(dish_id), "own", plan.id
    else:
        first = plan.id
        for anc_id, anc in chain:
            if _same_dish(anc, dish_id, name_key) is not None:
                first = anc_id
        key, origin = lineage_key(first, dish_id, name_key), "plan"
    rid = recipe_id_for(key)
    return rid, recipe_row(key, rid, dish, plan, origin=origin, first_plan_id=first, at=at)


def recipe_for(store: _Store, plan: PlanCtx, dish: dict, variants: dict,
               chain: Chain, at: datetime) -> str:
    """Рецепт блюда (правила dish_link; одни и те же для миграции и живого приложения):
    1. dish.recipe_id — если есть (раз закреплённое не пересчитываем);
    2. ближайший предок по parent_id с тем же блюдом (id и название) и рецептом;
    3. копия из Книги — к источнику при полном совпадении текста;
    4. иначе uuid5 от ключа линии 'lin:<первое появление>/<dish_id>/<name_key>' ('own:<id>' —
       свой рецепт), вставка INSERT OR IGNORE.
    Никогда — только по dish_id и никогда — только по названию."""
    name_key = norm_name(str(dish.get("name") or ""))
    pinned = dish.get("recipe_id")
    if pinned and store.has_recipe(pinned):
        return str(pinned)
    rid, row = _derive(store, plan, dish, name_key, variants, chain, at)
    if pinned:
        # Строки рецепта нет (снята миграция без strip) — восстанавливаем под тем же id.
        # Ключ не воспроизводится (корень линии удалён) — ключ из самого id, тоже детерминирован.
        if rid != pinned or row is None:
            first = row["origin_plan_id"] if row else plan.id
            origin = row["origin"] if row else ("own" if plan.library else "plan")
            row = recipe_row(f"pin:{pinned}", str(pinned), dish, plan, origin=origin,
                             first_plan_id=first, at=at)
            logger.warning("рецепт %s блюда %s/%s восстановлен по закреплению", pinned,
                           plan.id, dish.get("id"))
        rid = str(pinned)
    if row is not None and not store.has_recipe(rid):
        store.add_recipe(row)
    return rid


def pin_dish(store: _Store, plan: PlanCtx, dish: dict, chain: Chain,
             *, default_kind: str, at: datetime) -> dict:
    """Блюдо → то же блюдо с recipe_id и rev_ids (недостающие версии — в store). Блюдо без
    рецепта (только шапка, спека Cloudflare без провайдера) возвращается как есть.

    default_kind — вид версии для текста без метаданных варианта: 'migrated' (перенос
    миграцией) или 'resync' (текст, записанный старым кодом / мимо двойной записи).
    at — дата для версий без generated_at (помечаются как приблизительные)."""
    variants = variants_for(dish)
    if not variants:
        return dish
    rid = recipe_for(store, plan, dish, variants, chain, at)
    source = str(dish.get("source") or "")
    old_pins = dish.get("rev_ids") if isinstance(dish.get("rev_ids"), dict) else {}
    pins: dict[str, str] = {}
    for model, v in variants.items():
        provider = str(v.get("provider") or "")
        gen = str(v.get("generated_at") or "")
        chash = content_hash(v)
        rev_id = revision_id_for(rid, model, chash, meta_hash(provider, gen, source))
        pins[model] = rev_id
        if store.has_revision(rev_id, rid):
            continue
        has_meta = "kind" in v  # метаданные варианта пишутся с фазы 0a
        # От какой версии шли: прежнее закрепление слота-родителя (с фазы 0a — parent_id
        # варианта, ключ модели; без метаданных — прежняя версия этой же модели).
        parent_key = v.get("parent_id") if has_meta else model
        parent = old_pins.get(parent_key) if isinstance(parent_key, str) else None
        created = parse_generated_at(gen)
        store.add_revision({
            "id": rev_id, "recipe_id": rid, "model": model,
            "model_ref": str(v.get("model_ref") or ""), "provider": provider,
            "servings": _int(dish.get("servings"), 4),
            "ingredients": list(v.get("ingredients") or []),
            "steps": list(v.get("steps") or []), "tips": list(v.get("tips") or []),
            "note": str(v.get("note") or ""), "content_hash": chash, "source_used": source,
            "meta_hash": meta_hash(provider, gen, source),
            "kind": str(v.get("kind") or default_kind) if has_meta else default_kind,
            "change": str(v.get("change") or ""),
            "parent_id": parent if parent and parent != rev_id else None,
            "plan_id": plan.id, "dish_id": str(dish.get("id") or ""),
            "conversation_id": plan.conversation_id,
            "ctx_uses": [str(u) for u in ((v.get("ctx_uses") if "ctx_uses" in v
                                           else dish.get("uses")) or []) if u],
            "gen_id": str(v.get("gen_id") or ""), "generated_at": gen,
            "created_at": created or at, "created_at_estimated": created is None,
            "hidden_at": None,
        })
    out = dict(dish)
    out["recipe_id"] = rid
    out["rev_ids"] = pins
    return out


def pin_dishes(store: _Store, plan: PlanCtx, dishes: list[dict],
               chain: Chain, *, default_kind: str, at: datetime) -> list[dict]:
    return [pin_dish(store, plan, d, chain, default_kind=default_kind, at=at) for d in dishes]


def has_recipes(dishes: Iterable[dict]) -> bool:
    return any(variants_for(d) for d in dishes)


# --- двойная запись (services/planstore) ---


def enabled(session: Session) -> bool:
    """Миграция recipes_v1 применена (её маркер есть) → двойная запись включена. Без таблиц
    (миграцию не применяли, снята, тестовая база) — выключена: код ведёт себя как фаза 0."""
    has_table = session.exec(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
    )).first()
    if not has_table:
        return False
    return session.exec(
        text("SELECT 1 FROM schema_migration WHERE name = :n"), params={"n": MARKER}
    ).first() is not None


def _db_chain(session: Session, parent_id: str | None) -> list[tuple[str, list[dict]]]:
    """Предки версии плана по parent_id (ближайший первым), пока родитель существует."""
    out: list[tuple[str, list[dict]]] = []
    seen: set[str] = set()
    pid = parent_id
    while pid and pid not in seen:
        got = session.exec(
            select(PlanRow.parent_id, PlanRow.dishes).where(PlanRow.id == pid)
        ).first()
        if got is None:
            break
        seen.add(pid)
        out.append((pid, list(got[1] or [])))
        pid = got[0]
    return out


class _DbChain:
    """Предки из базы — читаются при первом обходе: нужны только блюду без recipe_id (правила
    2 и 4), а обычная запись уже закреплённых блюд не тянет JSON всей цепочки версий."""

    def __init__(self, session: Session, parent_id: str | None) -> None:
        self.s, self.parent_id = session, parent_id
        self._items: list[tuple[str, list[dict]]] | None = None

    def __iter__(self):
        if self._items is None:
            self._items = _db_chain(self.s, self.parent_id)
        return iter(self._items)


def sync_dishes(session: Session, plan: PlanCtx, parent_id: str | None,
                dishes: list[dict]) -> tuple[list[dict], _DbStore]:
    """Закрепить блюда версии плана за рецептами/версиями в базе (вставляет недостающее).
    Возвращает новый список блюд (только + recipe_id/rev_ids) и счётчики вставок.
    Дата версии без generated_at — дата версии плана (как у миграции: id от даты не зависят,
    но история показывает одно и то же «≈», каким бы путём текст ни попал в таблицы)."""
    store = _DbStore(session)
    store.prefetch(d.get("recipe_id") for d in dishes)
    pinned = pin_dishes(store, plan, dishes, _DbChain(session, parent_id),
                        default_kind="resync", at=plan.created_at)
    return pinned, store


def _plan_ctx(plan_id: str, conversation_id: str | None, status: str | None,
              created_at: datetime | None) -> PlanCtx:
    return PlanCtx(id=plan_id, conversation_id=conversation_id,
                   library=plan_id == _LIBRARY or status == _LIBRARY,
                   created_at=_naive_utc(created_at) if created_at else _now())


def dual_write(session: Session, plan_id: str, dishes: list[dict]) -> list[dict]:
    """Двойная запись после UPDATE блюд в planstore.patch_dishes (транзакция уже открыта этим
    UPDATE — SAVEPOINT внутри неё): версии в таблицы + закрепления в JSON вторым UPDATE.
    Сбой → откат SAVEPOINT, ERROR в лог; запись JSON остаётся (вылечит `migrations sync`).
    Возвращает итоговый список блюд (закреплённый или исходный)."""
    if not has_recipes(dishes) or not enabled(session):
        return dishes
    try:
        with session.begin_nested():
            got = session.exec(
                select(PlanRow.conversation_id, PlanRow.parent_id, PlanRow.status,
                       PlanRow.created_at).where(PlanRow.id == plan_id)
            ).first()
            if got is None:
                return dishes
            conv, parent, status, created = got
            pinned, store = sync_dishes(
                session, _plan_ctx(plan_id, conv, status, created), parent, dishes
            )
            if pinned != dishes:
                session.exec(
                    update(PlanRow).where(PlanRow.id == plan_id).values(dishes=pinned)
                    .execution_options(synchronize_session=False)
                )
    except Exception:  # noqa: BLE001 — таблицы не должны ронять запись плана
        _sync_total.labels(result="error").inc()
        logger.exception("рецепты: двойная запись плана %s не удалась — JSON записан, "
                         "закрепления догонит `python -m app.migrations sync`", plan_id)
        return dishes
    _sync_total.labels(result="ok").inc()
    if store.inserted_revisions or store.inserted_recipes:
        logger.info("рецепты: план %s +%d версий, +%d рецептов", plan_id,
                    store.inserted_revisions, store.inserted_recipes)
    return pinned


def dual_write_new(session: Session, row: PlanRow) -> None:
    """Двойная запись для новой строки плана (planstore.new_row): строка уже в сессии.
    flush — INSERT плана до SAVEPOINT (так SAVEPOINT вложен в транзакцию pysqlite, а не
    открывает её сам); сбой → откат SAVEPOINT, строка остаётся без закреплений."""
    dishes = list(row.dishes or [])
    if not has_recipes(dishes) or not enabled(session):
        return
    # Ошибка самой вставки плана (не таблиц рецептов) — наружу, как и без двойной записи.
    session.flush()
    try:
        with session.begin_nested():
            pinned, store = sync_dishes(
                session,
                _plan_ctx(row.id, row.conversation_id, row.status, row.created_at),
                row.parent_id, dishes,
            )
            if pinned != dishes:
                session.exec(
                    update(PlanRow).where(PlanRow.id == row.id).values(dishes=pinned)
                    .execution_options(synchronize_session=False)
                )
                session.expire(row, ["dishes"])
    except Exception:  # noqa: BLE001 — таблицы не должны ронять создание плана
        _sync_total.labels(result="error").inc()
        logger.exception("рецепты: двойная запись нового плана %s не удалась — JSON записан, "
                         "закрепления догонит `python -m app.migrations sync`", row.id)
        return
    _sync_total.labels(result="ok").inc()
    if store.inserted_revisions or store.inserted_recipes:
        logger.info("рецепты: новый план %s +%d версий, +%d рецептов", row.id,
                    store.inserted_revisions, store.inserted_recipes)


# --- ссылки оценок, избранного и реплик обсуждения на рецепт (поиск по ним — фаза 2+) ---

_REF_PK = {"ratingrow": "id", "favoriterecipe": "key", "messagerow": "id"}
_ref_tables = {
    name: table(name, column(_REF_PK[name]), *(column(c) for c in cols))
    for name, cols in RECIPE_REF_COLUMNS.items()
}


def dish_pins(session: Session, plan_id: str | None, dish_id: str | None) -> dict | None:
    """Блюдо dish_id версии плана plan_id с закреплением (или None)."""
    if not plan_id or not dish_id:
        return None
    got = session.exec(select(PlanRow.dishes).where(PlanRow.id == plan_id)).first()
    dish = next((d for d in (got or []) if d.get("id") == dish_id), None)
    return dish if dish and dish.get("recipe_id") else None


def _link(session: Session, name: str, key: str, values: dict) -> None:
    """UPDATE ссылки в SAVEPOINT поверх уже записанной (flush) строки: сбой не роняет саму
    оценку/реплику — только ERROR в лог (следующий `migrations sync` дозаполнит)."""
    t = _ref_tables[name]
    pk = t.c[_REF_PK[name]]
    try:
        with session.begin_nested():
            session.exec(update(t).where(pk == key).values(**values))
    except Exception:  # noqa: BLE001
        logger.exception("рецепты: не записали ссылку %s %s", name, key)


def link_rating(session: Session, rating_id: str, plan_id: str | None, dish_id: str | None,
                model: str) -> None:
    """👍/👎 рецепта → рецепт и ТА версия, за которую голосовали (закрепление модели голоса
    в этой версии плана). Строка оценки уже записана в этой транзакции (flush).

    Закреплению верим, только если оно указывает ровно на текст варианта в JSON: после сбоя
    двойной записи (SAVEPOINT откатился) у «↻»-нутого блюда остаётся прежнее rev_ids, и голос
    за новый текст лёг бы на старую версию — а sync такие (непустые) ссылки уже не правит.
    Не сходится или закрепления нет — ссылку ОБНУЛЯЕМ (сменённый голос мог хранить прежнюю),
    её дозаполнит `migrations sync` после перезакрепления."""
    if not enabled(session):
        return
    dish = dish_pins(session, plan_id, dish_id)
    rev = (dish.get("rev_ids") or {}).get(model) if dish else None
    v = variants_for(dish).get(model) if dish else None
    if dish and rev and v is not None and rev == pin_id(
            str(dish["recipe_id"]), model, v, str(dish.get("source") or "")):
        values = {"recipe_id": dish["recipe_id"], "revision_id": rev}
    else:
        if rev:
            logger.warning("рецепты: оценка %s — закрепление %s/%s/%s не совпадает с текстом "
                           "(сбой двойной записи?), ссылку допишет migrations sync",
                           rating_id, plan_id, dish_id, model)
        values = {"recipe_id": None, "revision_id": None}
    _link(session, "ratingrow", rating_id, values)


def link_favorite(session: Session, key: str, plan_id: str | None, dish_id: str | None) -> None:
    if not enabled(session):
        return
    dish = dish_pins(session, plan_id, dish_id)
    if dish:
        _link(session, "favoriterecipe", key, {"recipe_id": dish["recipe_id"]})


def link_message(session: Session, message_id: str, recipe_id: str | None) -> None:
    if recipe_id and enabled(session):
        _link(session, "messagerow", message_id, {"recipe_id": recipe_id})


# --- чтение из таблиц (фаза 1 — для сверки и тестов; фаза 2 — основное чтение) ---


def load_revisions(session: Session, ids: Iterable[str]) -> dict[str, RecipeRevision]:
    """Версии по id — одним IN-запросом на 500 id."""
    want = sorted({i for i in ids if i})
    out: dict[str, RecipeRevision] = {}
    for i in range(0, len(want), 500):
        chunk = want[i:i + 500]
        for r in session.exec(select(RecipeRevision).where(RecipeRevision.id.in_(chunk))).all():
            out[r.id] = r
    return out


def _variant_of(r: RecipeRevision) -> dict:
    return {
        "ingredients": list(r.ingredients or []), "steps": list(r.steps or []),
        "tips": list(r.tips or []), "note": r.note or "", "provider": r.provider or "",
        "generated_at": r.generated_at or "", "revision_id": r.id, "model_ref": r.model_ref or "",
    }


def hydrate_dish(dish: dict, revs: dict[str, RecipeRevision]) -> dict:
    """Блюдо из закреплений → тот же dict, что в JSON: варианты по закреплениям (в порядке
    rev_ids) и плоские поля активного через services.variants.apply_variant; source — текст
    своего рецепта, по которому писали активную версию. Без закреплений — блюдо как есть.
    Закрепление на отсутствующую версию → JSON-копия блюда + метрика + ERROR (целостность
    хранилища, а не подмена модели)."""
    pins = dish.get("rev_ids") or {}
    if not pins:
        return dict(dish)
    variants: dict[str, dict] = {}
    for model, rev_id in pins.items():
        r = revs.get(rev_id)
        if r is None or r.recipe_id != dish.get("recipe_id") or r.model != model:
            _hydrate_miss.inc()
            logger.error("рецепты: блюдо %s закреплено на версию %s (%s), которой нет — "
                         "отдаём JSON блюда", dish.get("id"), rev_id, model)
            return dict(dish)
        variants[model] = _variant_of(r)
    active = dish.get("active_model")
    if active not in variants:
        active = next(iter(variants))
    out = apply_variant(dish, active, variants)
    source = revs[pins[active]].source_used or ""
    if source or "source" in dish:
        out["source"] = source
    else:
        out.pop("source", None)
    return out


def hydrate_rows(session: Session, rows: Iterable) -> dict[str, list[dict]]:
    """Блюда нескольких версий плана из таблиц — один IN-запрос на все закрепления.
    rows — PlanRow или любые объекты с .id и .dishes."""
    rows = list(rows)
    revs = load_revisions(session, (
        rid for row in rows for d in (row.dishes or [])
        for rid in (d.get("rev_ids") or {}).values()
    ))
    return {row.id: [hydrate_dish(d, revs) for d in (row.dishes or [])] for row in rows}


@dataclass
class BookEntry:
    """Видимый рецепт Книги: свой или закреплённый принятым планом; ссылка — самое свежее
    принятое блюдо с ним (свой рецепт — блюдо «Моих рецептов»)."""

    recipe_id: str
    origin: str
    plan_id: str
    dish_id: str
    name_key: str
    dish: dict  # блюдо-ссылка, восстановленное из таблиц


Book = tuple[list[PlanRow], dict[str, list[dict]]]


def book_hydrated(session: Session) -> Book:
    """Планы Книги в её порядке (= recipebook.book_rows: свои первыми, дальше принятые по
    decided_at, иначе created_at, свежие первыми) и их блюда из таблиц (незакреплённые — как в
    JSON). recipebook не импортируем: он импортирует planstore → сюда."""
    rows = session.exec(
        select(PlanRow).where(PlanRow.status.in_(("accepted", _LIBRARY)))
    ).all()
    rows.sort(key=lambda r: (r.status == _LIBRARY, r.decided_at or r.created_at), reverse=True)
    return rows, hydrate_rows(session, rows)


def book_entries(session: Session, book: Book | None = None) -> list[BookEntry]:
    """Книга рецептов по таблицам: свои первыми, дальше по свежести принятого плана-ссылки
    (decided_at, иначе created_at), внутри плана — в порядке блюд. Один рецепт — одна запись
    (самая свежая ссылка), черновики и отклонённые версии в Книгу не попадают."""
    rows, hydrated = book or book_hydrated(session)
    out: list[BookEntry] = []
    seen: set[str] = set()
    for row in rows:
        for d in hydrated[row.id]:
            rid = d.get("recipe_id")
            if not rid or rid in seen or not d.get("id"):
                continue
            seen.add(rid)
            out.append(BookEntry(
                recipe_id=str(rid), origin="own" if row.status == _LIBRARY else "plan",
                plan_id=row.id, dish_id=str(d["id"]),
                name_key=norm_name(str(d.get("name") or "")), dish=d,
            ))
    return out


def book_index_tables(session: Session, book: Book | None = None) -> dict[str, dict]:
    """norm_name → блюдо с рецептом — как recipebook.book_index (те же планы, порядок и
    фильтр), но закреплённые блюда восстановлены из таблиц. Незакреплённое блюдо с телом в
    JSON (своё в плане: шаги без провайдера/вариантов) остаётся как есть — оно в Книге и до
    миграции, и после (такие блюда держат тело в JSON всегда)."""
    rows, hydrated = book or book_hydrated(session)
    out: dict[str, dict] = {}
    for row in rows:
        for d in hydrated[row.id]:
            key = norm_name(str(d.get("name") or ""))
            if key and key not in out and d.get("steps") and d.get("ingredients"):
                out[key] = d
    return out


# --- состояние для /api/health (проверка на старте, только чтение) ---

_health: dict = {"marker": False, "store": "json"}


def startup_check(engine) -> dict:
    """На старте: есть ли маркер и сходятся ли таблицы с JSON (verify только на чтение,
    ничего не пишет). Фаза 1 читает JSON при любом исходе — это сигнал в лог и /api/health."""
    global _health
    state: dict = {"marker": False, "store": "json"}
    try:
        with Session(engine) as s:
            state["marker"] = enabled(s)
            if state["marker"]:
                from ..migrations import recipes_v1  # лениво: CLI-пакет тянет сверку целиком

                report = recipes_v1.verify_session(s, strict_refs=False)
                state["verify"] = {"ok": report["ok"], "failed": report["failed"]}
                if report["ok"]:
                    logger.info("рецепты: таблицы сходятся с JSON (%d проверок)",
                                len(report["checks"]))
                else:
                    logger.error("рецепты: сверка таблиц с JSON не прошла: %s",
                                 ", ".join(report["failed"]))
            s.rollback()
    except Exception as exc:  # noqa: BLE001 — сверка на старте не должна ронять приложение
        logger.exception("рецепты: сверка на старте упала")
        state["verify"] = {"ok": False, "failed": ["exception"], "error": str(exc)[:200]}
    _health = state
    return state


def health() -> dict:
    return dict(_health)
