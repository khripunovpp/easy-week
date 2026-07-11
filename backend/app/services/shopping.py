import re

from ..schemas import Dish, ShoppingGroup, ShoppingItem

CATEGORY_ORDER = [
    "Мясо и птица",
    "Рыба",
    "Овощи",
    "Молочное",
    "Бакалея",
    "Специи",
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


def group_items(items: list[dict]) -> list[ShoppingGroup]:
    """Группирует позиции по категориям в заданном порядке."""
    by_cat: dict[str, list[ShoppingItem]] = {}
    for it in items:
        cat = it.get("category") or "Прочее"
        by_cat.setdefault(cat, []).append(
            ShoppingItem(
                name=str(it.get("name", "")).strip(),
                qty=it.get("qty", 0),
                unit=str(it.get("unit", "")).strip(),
                category=cat,
            )
        )
    order = CATEGORY_ORDER + [c for c in by_cat if c not in CATEGORY_ORDER]
    return [
        ShoppingGroup(category=c, items=sorted(by_cat[c], key=lambda x: x.name.lower()))
        for c in order
        if c in by_cat
    ]


def build_shopping_list(dishes: list[Dish]) -> list[ShoppingGroup]:
    """Детерминированный список покупок (фолбэк, без модели)."""
    return group_items(aggregate_ingredients(dishes))
