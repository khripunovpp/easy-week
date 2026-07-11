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


def build_shopping_list(dishes: list[Dish]) -> list[ShoppingGroup]:
    """Суммирует ингредиенты по (name+unit) и группирует по категориям."""
    merged: dict[str, ShoppingItem] = {}
    for dish in dishes:
        for ing in dish.ingredients:
            key = f"{ing.name.lower()}__{ing.unit}"
            if key in merged:
                merged[key].qty += ing.qty
            else:
                merged[key] = ShoppingItem(
                    name=ing.name, qty=ing.qty, unit=ing.unit, category=ing.category
                )

    by_cat: dict[str, list[ShoppingItem]] = {}
    for item in merged.values():
        by_cat.setdefault(item.category, []).append(item)

    order = CATEGORY_ORDER + [c for c in by_cat if c not in CATEGORY_ORDER]
    groups: list[ShoppingGroup] = []
    for cat in order:
        if cat in by_cat:
            items = sorted(by_cat[cat], key=lambda x: x.name.lower())
            groups.append(ShoppingGroup(category=cat, items=items))
    return groups
