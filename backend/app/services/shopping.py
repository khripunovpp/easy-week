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

# Приведение единиц к базовым, чтобы «1 кг» и «1400 г» суммировались вместе.
_UNIT_BASE: dict[str, tuple[str, float]] = {
    "г": ("г", 1),
    "гр": ("г", 1),
    "грамм": ("г", 1),
    "g": ("г", 1),
    "кг": ("г", 1000),
    "kg": ("г", 1000),
    "мл": ("мл", 1),
    "ml": ("мл", 1),
    "л": ("мл", 1000),
    "l": ("мл", 1000),
    "литр": ("мл", 1000),
}


def _to_base(unit: str, qty: float) -> tuple[str, float]:
    base, factor = _UNIT_BASE.get(unit.strip().lower(), (unit.strip(), 1))
    return base, qty * factor


def _present(base_unit: str, qty: float) -> tuple[float, str]:
    # Крупные количества — в кг/л для читаемости.
    if base_unit == "г" and qty >= 1000:
        return round(qty / 1000, 2), "кг"
    if base_unit == "мл" and qty >= 1000:
        return round(qty / 1000, 2), "л"
    return round(qty, 2) if qty % 1 else int(qty), base_unit


def build_shopping_list(dishes: list[Dish]) -> list[ShoppingGroup]:
    """Суммирует ингредиенты по названию с нормализацией единиц, группирует по категориям."""
    merged: dict[str, dict] = {}
    for dish in dishes:
        for ing in dish.ingredients:
            base_unit, base_qty = _to_base(ing.unit, ing.qty)
            key = f"{ing.name.strip().lower()}__{base_unit}"
            if key in merged:
                merged[key]["qty"] += base_qty
            else:
                merged[key] = {
                    "name": ing.name.strip(),
                    "base_unit": base_unit,
                    "qty": base_qty,
                    "category": ing.category,
                }

    by_cat: dict[str, list[ShoppingItem]] = {}
    for m in merged.values():
        qty, unit = _present(m["base_unit"], m["qty"])
        item = ShoppingItem(name=m["name"], qty=qty, unit=unit, category=m["category"])
        by_cat.setdefault(item.category, []).append(item)

    order = CATEGORY_ORDER + [c for c in by_cat if c not in CATEGORY_ORDER]
    groups: list[ShoppingGroup] = []
    for cat in order:
        if cat in by_cat:
            items = sorted(by_cat[cat], key=lambda x: x.name.lower())
            groups.append(ShoppingGroup(category=cat, items=items))
    return groups
