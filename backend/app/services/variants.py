"""Варианты рецепта блюда по моделям: dish["variants"] = {model_key: {...}} + active_model.

Плоские поля блюда (ingredients/steps/tips/detail_provider/storage.note) — зеркало активного
варианта (их читают покупки/PDF/план готовки). Хелперы общие для роутера деталей
(routers/plans.py), правки рецепта в чате (ai/planner.edit_plan → edit_dish), обсуждения,
догенерации (services/regenerate.backfill_all) и своего рецепта.

Каждый НОВЫЙ вариант несёт метаданные (кто и как сделал этот текст) — для истории версий
и переноса рецептов в отдельные таблицы (без них миграция ставит kind=migrated). Старый код
их не читает (pydantic отбрасывает лишние ключи):
- model_ref — точная модель «провайдер:id» (gates.model_ref); ключ варианта — провайдер;
- kind — VARIANT_KINDS: generate (первое открытие/выбор модели), regenerate («↻», в т.ч.
  с «Что учесть?»), chat_edit (правка блюда в чате), discuss_edit (правка из обсуждения),
  backfill (догенерация для покупок/PDF/готовки), custom (свой рецепт);
- change — уточнение «Что учесть?» / правка из чата или обсуждения (пусто — без правки);
- parent_id — ключ варианта, от которого шли (прежний вариант этой модели, иначе активный;
  None — рецепта ещё не было);
- ctx_uses — остатки плана, под которые писали (dish.uses на момент генерации);
- gen_id — id вызова модели в AI-логе (ai-*.jsonl), см. ai/observe.
"""

from datetime import datetime, timezone

from ..ai.gates import GATES


def now_iso() -> str:
    """Время генерации для JSON-кэшей (варианты рецепта/плана готовки): ISO-строка в UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# Провайдер (человекочитаемый) → ключ модели — для миграции legacy-детали в вариант.
_PROVIDER_KEY = {g.provider: g.key for g in GATES.values()}


VARIANT_KINDS = ("generate", "regenerate", "chat_edit", "discuss_edit", "backfill", "custom")


def parent_key(dish: dict, model: str) -> str | None:
    """От какого варианта строится новый вариант model: прежний вариант этой модели (↻/правка),
    иначе активный; None — рецепта у блюда ещё не было."""
    variants = dish_variants(dish)
    if model in variants:
        return model
    active = dish.get("active_model")
    if active in variants:
        return active
    return next(iter(variants), None)


def variant_from_detail(
    detail: dict, *, kind: str, change: str = "", parent_id: str | None = None,
    ctx_uses: list | None = None,
) -> dict:
    """Деталь модели → вариант рецепта (ингредиенты/шаги/советы/note/провайдер) + метаданные
    генерации (см. docstring модуля). kind обязателен — каждая точка записи называет себя."""
    if kind not in VARIANT_KINDS:
        raise ValueError(f"неизвестный kind варианта: {kind}")
    return {
        "ingredients": detail.get("ingredients") or [],
        "steps": detail.get("steps") or [],
        "tips": detail.get("tips") or [],
        "note": detail.get("note") or "",
        "provider": detail.get("provider") or "",
        "generated_at": now_iso(),
        "model_ref": detail.get("model_ref") or "",
        "kind": kind,
        "change": (change or "").strip(),
        "parent_id": parent_id,
        "ctx_uses": [str(u) for u in (ctx_uses or []) if u],
        "gen_id": detail.get("gen_id") or "",
    }


def dish_variants(dish: dict) -> dict:
    """Варианты рецепта по моделям; при отсутствии — мигрируем плоскую деталь (legacy)."""
    variants = dict(dish.get("variants") or {})
    if not variants and (dish.get("ingredients") or dish.get("steps")):
        key = _PROVIDER_KEY.get(dish.get("detail_provider", ""))
        if key:
            variants[key] = {
                "ingredients": dish.get("ingredients") or [],
                "steps": dish.get("steps") or [],
                "tips": dish.get("tips") or [],
                "note": (dish.get("storage") or {}).get("note") or "",
                "provider": dish.get("detail_provider") or "",
            }
    return variants


def apply_variant(dish: dict, model: str, variants: dict) -> dict:
    """Делает вариант model активным: зеркалим его в плоские поля (для покупок/PDF)."""
    v = variants.get(model) or {}
    return {
        **dish,
        "variants": variants,
        "active_model": model,
        "ingredients": v.get("ingredients") or [],
        "steps": v.get("steps") or [],
        "tips": v.get("tips") or [],
        "detail_provider": v.get("provider") or "",
        # Когда сгенерирован активный вариант (пусто у старых данных — подпись не показываем).
        "detail_generated_at": v.get("generated_at") or "",
        "storage": {**(dish.get("storage") or {}), "note": v.get("note") or ""},
    }


def with_detail(
    dish: dict, model: str, detail: dict, *, kind: str, change: str = "",
    basis: dict | None = None,
) -> dict:
    """Записать свежую деталь как вариант model и сделать его активным (одной операцией).

    dish — блюдо, В КОТОРОЕ пишем (при записи — свежее из БД, services/planstore), basis —
    блюдо, ПО КОТОРОМУ генерили (снимок до вызова модели): от него parent_id и ctx_uses.
    Пусто — то же dish."""
    base = dish if basis is None else basis
    variants = dish_variants(dish)
    variants[model] = variant_from_detail(
        detail, kind=kind, change=change, parent_id=parent_key(base, model),
        ctx_uses=base.get("uses"),
    )
    return apply_variant(dish, model, variants)


def variant_summary(dish: dict, cap: int = 400) -> str:
    """Короткая выжимка активного варианта для промпта перегенерации: ключевые ингредиенты
    + число шагов. Держим компактной, чтобы не раздувать токены."""
    ings = [str(i.get("name", "")).strip() for i in (dish.get("ingredients") or []) if i.get("name")]
    if not ings:
        return ""
    text = "ингредиенты: " + ", ".join(ings[:14])
    steps = len(dish.get("steps") or [])
    if steps:
        text += f"; шагов: {steps}"
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"
