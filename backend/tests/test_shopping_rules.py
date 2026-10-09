"""Покупки: детерминированные правила (яйца, зелень, вода) и количества кодом из базы
(👎 2026-10-09: яйца в двух отделах, зелень в «Прочем», рис/лук с неверными суммами)."""

from types import SimpleNamespace

from app.schemas import Ingredient
from app.services import shopping


def _dish(*ings):
    # aggregate_ingredients смотрит только на ingredients
    return SimpleNamespace(
        ingredients=[Ingredient(name=n, qty=q, unit=u, category=c) for n, q, u, c in ings])


def test_eggs_merge_and_go_to_dairy():
    items = shopping.aggregate_ingredients([
        _dish(("яйцо", 1, "шт", "Молочное")),
        _dish(("Яйца куриные", 4, "шт", "Прочее")),
    ])
    assert items == [{"name": "Яйцо", "qty": 5, "unit": "шт", "category": "Молочное"}]
    assert shopping._canon_name("яйца перепелиные") != "яйц"


def test_herbs_to_vegetables_dried_untouched_water_dropped():
    items = shopping.aggregate_ingredients([_dish(
        ("зелень свежая", 20, "г", "Прочее"),
        ("укроп", 10, "г", "Прочее"),
        ("базилик сушёный", 2, "г", "Специи"),
        ("вода", 2000, "мл", "Прочее"),
        ("кипяток", 200, "мл", "Прочее"),
    )])
    cats = {i["name"]: i["category"] for i in items}
    assert cats == {"Зелень свежая": "Овощи", "Укроп": "Овощи", "Базилик сушёный": "Специи"}


def test_group_items_fixes_old_cache():
    groups = shopping.group_items([
        {"name": "яйца куриные", "qty": 4, "unit": "шт", "category": "Прочее"},
        {"name": "вода", "qty": 2, "unit": "л", "category": "Прочее"},
    ])
    assert [(g.category, [i.name for i in g.items]) for g in groups] == [
        ("Молочное", ["яйца куриные"])]


BASE = [
    {"name": "Рис", "qty": 60, "unit": "г", "category": "Бакалея"},
    {"name": "Рис белый", "qty": 100, "unit": "г", "category": "Бакалея"},
    {"name": "Лук", "qty": 250, "unit": "г", "category": "Овощи"},
    {"name": "Лук репчатый", "qty": 400, "unit": "г", "category": "Овощи"},
    {"name": "Чеснок", "qty": 2, "unit": "шт", "category": "Овощи"},
    {"name": "Соль", "qty": 10, "unit": "г", "category": "Специи"},
]


def test_reconcile_sums_from_base_not_model():
    got = [
        {"name": "рис", "qty": 160, "unit": "г", "category": "Бакалея", "from": [1, 2]},
        {"name": "рис белый", "qty": 100, "unit": "г", "category": "Бакалея", "from": [2]},
        {"name": "лук", "qty": 750, "unit": "г", "category": "Овощи", "from": [3, 4]},
        {"name": "чеснок", "qty": 3, "unit": "шт", "category": "Овощи"},  # без from — по названию
        {"name": "сахар", "qty": 5, "unit": "г", "category": "Бакалея"},  # выдумка
    ]
    out = shopping.reconcile_normalized(BASE, got)
    assert out == [
        {"name": "рис", "qty": 160, "unit": "г", "category": "Бакалея"},
        {"name": "лук", "qty": 650, "unit": "г", "category": "Овощи"},  # не 750
        {"name": "чеснок", "qty": 2, "unit": "шт", "category": "Овощи"},
        {"name": "Соль", "qty": 10, "unit": "г", "category": "Специи"},  # модель потеряла — вернули
    ]


def test_reconcile_mixed_units_and_kg():
    base = [{"name": "Морковь", "qty": 1.2, "unit": "кг", "category": "Овощи"},
            {"name": "Морковь", "qty": 2, "unit": "шт", "category": "Овощи"}]
    out = shopping.reconcile_normalized(
        base, [{"name": "морковь", "qty": 1, "unit": "кг", "category": "Овощи", "from": [1, 2]}])
    assert out == [{"name": "морковь", "qty": 1.2, "unit": "кг", "category": "Овощи"},
                   {"name": "морковь", "qty": 2, "unit": "шт", "category": "Овощи"}]


def test_reconcile_with_discussion_allows_removal_and_unit_change():
    got = [{"name": "лук", "qty": 4, "unit": "шт", "category": "Овощи", "from": [3, 4]}]
    out = shopping.reconcile_normalized(BASE, got, keep_missing=False)
    assert out == [{"name": "лук", "qty": 4, "unit": "шт", "category": "Овощи"}]
