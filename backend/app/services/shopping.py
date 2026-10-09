import re
from uuid import uuid4

from ..schemas import Dish, ShoppingGroup, ShoppingItem

# Категории рецептов + отделы, которые встречаются только у своих товаров (хлеб, фрукты,
# напитки, хозтовары) — порядок групп в списке. Рецепты пишут только первые семь.
CATEGORY_ORDER = [
    "Мясо и птица",
    "Рыба",
    "Овощи",
    "Фрукты",
    "Молочное",
    "Хлеб и выпечка",
    "Бакалея",
    "Специи",
    "Напитки",
    "Хозтовары",
    "Прочее",
]

# Единицы → базовые (ключ нормализован _norm_unit: без точек/пробелов, ё→е).
# Весовое сводим к граммам, жидкости — к мл, штучное остаётся 'шт'.
_UNIT_BASE: dict[str, tuple[str, float]] = {
    "г": ("г", 1), "гр": ("г", 1), "грамм": ("г", 1), "граммов": ("г", 1), "g": ("г", 1),
    "кг": ("г", 1000), "kg": ("г", 1000), "килограмм": ("г", 1000),
    "мл": ("мл", 1), "ml": ("мл", 1),
    "л": ("мл", 1000), "литр": ("мл", 1000), "l": ("мл", 1000),
    "чл": ("г", 5), "чайнаяложка": ("г", 5), "чложка": ("г", 5), "чайнойложки": ("г", 5),
    "стл": ("г", 15), "столоваяложка": ("г", 15), "стложка": ("г", 15), "столовойложки": ("г", 15),
    "щепотка": ("г", 1), "щепоть": ("г", 1), "стакан": ("г", 200), "стакана": ("г", 200),
    "зубчик": ("г", 5), "зубчика": ("г", 5), "зубчиков": ("г", 5),
    "долька": ("г", 5), "дольки": ("г", 5),
}

# Слова-шумы, которые не должны мешать объединению одинаковых продуктов.
_NOISE = {"молотый", "молотая", "свежемолотый", "свежий", "свежая", "сушёный", "сушеный"}


def _norm_unit(unit: str) -> str:
    return re.sub(r"[.\s]", "", unit.lower().replace("ё", "е").strip())


def _to_base(unit: str, qty: float) -> tuple[str, float]:
    base, factor = _UNIT_BASE.get(_norm_unit(unit), (unit.strip(), 1))
    return base, qty * factor


def _canon_name(name: str) -> str:
    """Каноничный ключ: нижний регистр, ё→е, сортировка слов, без шумовых слов.

    Так «Перец чёрный», «чёрный перец», «Чёрный перец молотый» сливаются в один.
    """
    s = name.lower().replace("ё", "е").strip()
    s = re.sub(r"[^а-я0-9 ]", " ", s)
    toks = []
    for t in s.split():
        if not t or t in _NOISE:
            continue
        # лёгкий стемминг ед./мн. числа: «помидоры»→«помидор», «бобы»→«боб»
        if len(t) >= 4 and t[-1] in "ыи":
            t = t[:-1]
        toks.append(t)
    return " ".join(sorted(toks))


def _present(base_unit: str, qty: float):
    if base_unit == "г" and qty >= 1000:
        return round(qty / 1000, 2), "кг"
    if base_unit == "мл" and qty >= 1000:
        return round(qty / 1000, 2), "л"
    return (round(qty, 2) if qty % 1 else int(qty)), base_unit


def aggregate_ingredients(dishes: list[Dish]) -> list[dict]:
    """База: объединяет формы одного продукта (canon), всё весовое — в граммах."""
    merged: dict[str, dict] = {}
    for dish in dishes:
        for ing in dish.ingredients:
            base_unit, base_qty = _to_base(ing.unit, ing.qty)
            key = f"{_canon_name(ing.name)}__{base_unit}"
            if key in merged:
                merged[key]["qty"] += base_qty
            else:
                name = ing.name.strip()
                merged[key] = {
                    "name": name[:1].upper() + name[1:],
                    "base_unit": base_unit,
                    "qty": base_qty,
                    "category": ing.category,
                }
    items: list[dict] = []
    for m in merged.values():
        qty, unit = _present(m["base_unit"], m["qty"])
        items.append({"name": m["name"], "qty": qty, "unit": unit, "category": m["category"]})
    return items


# Остатки плана (PlanRow.leftovers) — «пристроить, чтобы не пропали»: в покупках такие позиции
# уходят в отдельную группу в конце — проверить дома, а не покупать заново.
HOME_CATEGORY = "Есть дома (остатки)"

# Другой продукт из того же сырья: «оливковое масло» ≠ «оливки», «томатная паста» ≠ «томаты»,
# «лук-порей» ≠ «лук», «имбирь молотый» ≠ свежий. Такие слова в позиции (если их нет в самом
# остатке) отменяют совпадение.
_DERIVED = ("масл", "сок", "паст", "соус", "порош", "молот", "сушен", "порей", "шалот")


def _raw_tokens(name: str) -> list[str]:
    s = re.sub(r"[^а-я0-9 ]", " ", name.lower().replace("ё", "е"))
    return s.split()


def _tok_match(a: str, b: str) -> bool:
    # Точно или та же основа с другим окончанием: «имбирь»/«имбиря», «сельдерей»/«сельдерея».
    return a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5] and abs(len(a) - len(b)) <= 2)


def is_leftover(name: str, leftovers: list[str] | None) -> bool:
    """Позиция покупок — это один из остатков пользователя (все слова остатка есть в позиции,
    и позиция не производный продукт вроде масла/пасты/сока из него)."""
    toks = _canon_name(name).split()
    raw = _raw_tokens(name)
    for lo in leftovers or []:
        lt = _canon_name(lo).split()
        if not lt or not all(any(_tok_match(x, t) for t in toks) for x in lt):
            continue
        lo_raw = _raw_tokens(lo)
        derived = any(
            t.startswith(d) and not any(x.startswith(d) for x in lo_raw)
            for t in raw for d in _DERIVED
        )
        if not derived:
            return True
    return False


def sync_uses(dish: dict, leftovers: list[str] | None) -> dict:
    """Рецепт уже есть → uses = остатки, которые реально в его ингредиентах: план мог ошибиться
    (оливки «закуской» к котлетам по-киевски), а рецепт — отказаться от неуместного остатка.
    Без рецепта uses — намерение плана, как есть."""
    names = [str(i.get("name", "")) for i in (dish.get("ingredients") or [])]
    if not leftovers or not names:
        return dish
    uses = [lo for lo in leftovers if any(is_leftover(n, [lo]) for n in names)]
    return {**dish, "uses": uses}


def group_items(
    items: list[dict], leftovers: list[str] | None = None, extras: list[dict] | None = None
) -> list[ShoppingGroup]:
    """Группирует позиции по категориям в заданном порядке; остатки плана — в группу
    «Есть дома» последней. extras — свои товары (PlanRow.shopping_extras): в свои категории
    рядом с продуктами рецептов, с пометкой extra; в «Есть дома» не уходят — их просили купить."""
    by_cat: dict[str, list[ShoppingItem]] = {}
    for it in items:
        cat = it.get("category") or "Прочее"
        if leftovers and is_leftover(str(it.get("name", "")), leftovers):
            cat = HOME_CATEGORY
        by_cat.setdefault(cat, []).append(
            ShoppingItem(
                name=str(it.get("name", "")).strip(),
                qty=it.get("qty", 0),
                unit=str(it.get("unit", "")).strip(),
                category=cat,
            )
        )
    for it in extras or []:
        cat = it.get("category") or "Прочее"
        by_cat.setdefault(cat, []).append(
            ShoppingItem(
                name=str(it.get("name", "")).strip(),
                qty=it.get("qty", 0),
                unit=str(it.get("unit", "")).strip(),
                category=cat,
                extra=True,
                id=str(it.get("id", "")),
            )
        )
    order = CATEGORY_ORDER + [c for c in by_cat if c not in CATEGORY_ORDER and c != HOME_CATEGORY]
    order.append(HOME_CATEGORY)
    return [
        ShoppingGroup(category=c, items=sorted(by_cat[c], key=lambda x: x.name.lower()))
        for c in order
        if c in by_cat
    ]


# --- Свои товары («мимо рецептов»: хлеб, йогурт…) ---
# Пользователь пишет их свободным текстом, модель задачи «Список покупок» раскладывает по
# отделам (planner.parse_shopping_extras), здесь — чистка ответа и слияние с уже добавленными.

EXTRAS_MAX = 100
_EXTRA_UNITS = {"г", "кг", "мл", "л", "шт"}


def clean_extras(items: list) -> list[dict]:
    """Ответ модели → позиции {id, name, qty, unit, category}: имя с маленькой буквы, количество —
    только названное (иначе qty 0 и без единицы), единица из короткого набора, отдел — из
    CATEGORY_ORDER (незнакомый — «Прочее»). Одинаковые позиции ответа сливаются."""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = " ".join(str(it.get("name") or "").split())[:60]
        if not _canon_name(name):
            continue
        try:
            qty = float(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        unit = _norm_unit(str(it.get("unit") or ""))
        if qty <= 0 or unit not in _EXTRA_UNITS:
            qty, unit = 0.0, ""
        cat = str(it.get("category") or "").strip()
        out.append({
            "id": uuid4().hex[:12],
            "name": name[:1].lower() + name[1:] if not name[:2].isupper() else name,
            "qty": int(qty) if qty == int(qty) else round(qty, 2),
            "unit": unit,
            "category": cat if cat in CATEGORY_ORDER else "Прочее",
        })
    return merge_extras([], out)


def merge_extras(old: list[dict] | None, new: list[dict]) -> list[dict]:
    """Добавить новые свои товары к уже добавленным: тот же продукт (каноничное имя) с той же
    единицей — количества складываются («ещё молоко 1 л»), повтор без количества не дублируется,
    количество у позиции без него — подставляется. Разные единицы — отдельные строки."""
    out = [dict(it) for it in (old or [])]
    for it in new:
        key = _canon_name(str(it.get("name", "")))
        same = next(
            (o for o in out if _canon_name(str(o.get("name", ""))) == key
             and (o.get("unit") == it.get("unit") or not o.get("qty") or not it.get("qty"))),
            None,
        )
        if same is None:
            out.append(dict(it))
        elif it.get("qty"):
            if same.get("qty") and same.get("unit") == it.get("unit"):
                total = float(same["qty"]) + float(it["qty"])
                same["qty"] = int(total) if total == int(total) else round(total, 2)
            else:
                same["qty"], same["unit"] = it["qty"], it["unit"]
    return out[:EXTRAS_MAX]


def build_shopping_list(dishes: list[Dish]) -> list[ShoppingGroup]:
    """Детерминированный список покупок (фолбэк, без модели)."""
    return group_items(aggregate_ingredients(dishes))
