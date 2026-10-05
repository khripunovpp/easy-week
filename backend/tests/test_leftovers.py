"""Остатки «пристроить, чтобы не пропали» и книга рецептов семьи.

Разбор чата 2026-10-02: «остался порей, сельдерей, морковь… одно из блюд — начинка для пот-пая,
она уже есть в рецептах» → модель строила меню вокруг остатков (блюда-перечисления), а пот-пай
из-за «недавно ели» превратился в выдуманную «овощную с ячменем»."""

import asyncio

from app.ai import planner
from app.ai.prompt import (
    DEEPSEEK_PLAN_SYSTEM,
    NAMES_SYSTEM,
    build_dish_detail_messages,
    build_ds_plan_messages,
    build_single_dish_messages,
    free_leftovers,
    leftovers_status,
)
from app.ai.stream_parse import PlanStreamParser
from app.models import Conversation, FavoriteRecipe, PlanRow
from app.services import recipebook
from app.services.shopping import HOME_CATEGORY, group_items, is_leftover, sync_uses

LEFT = ["порей", "сельдерей", "морковь", "лук", "оливки", "имбирь", "солёные огурцы"]


class FakeGate:
    provider = "Fake"
    key = "fake"
    supports_stream = False
    supports_tools = False

    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    async def complete_json(self, messages, **kw):
        self.calls.append((messages, kw))
        return self.parsed, {}


# --- промпты ---------------------------------------------------------------------------------

def test_plan_system_treats_leftovers_as_ingredients_not_theme():
    for system in (DEEPSEEK_PLAN_SYSTEM, NAMES_SYSTEM):
        assert "ОСТАТКИ" in system and "НЕ тема меню" in system
        assert "а не конструкция «основа с остатком и остатком»" in system
        assert "пристроить все — не цель" in system
        assert "в reply НЕ расписывай" in system  # куда ушли — показывает карточка по uses
        assert "даже если оно есть в списке «недавно ели»" in system
    assert '"leftovers"' in DEEPSEEK_PLAN_SYSTEM and '"uses"' in DEEPSEEK_PLAN_SYSTEM


def test_plan_user_has_book_and_known_leftovers():
    msgs = build_ds_plan_messages(
        "меню", ["Начинка для чикен пот пай"], 5,
        book=["Начинка для чикен пот пай", "Борщ"], leftovers=["порей"],
    )
    assert msgs[0]["content"] == DEEPSEEK_PLAN_SYSTEM  # system стабилен
    user = msgs[1]["content"]
    assert "Книга рецептов семьи" in user and "НЕ подсказка меню" in user
    assert "Остатки пользователя — пристрой по правилу ОСТАТКИ" in user and "порей" in user
    assert "которые пользователь назвал сам, — можно" in user


def test_detail_uses_only_dish_leftovers():
    dish = {"name": "Курица с пореем", "uses": ["порей"]}
    user = build_dish_detail_messages("Курица с пореем", 4, dish=dish, leftovers=LEFT)[1]["content"]
    assert "Остатки пользователя, намеченные в это блюдо: порей" in user
    assert "только те, что естественно входят в классический рецепт" in user
    assert "ни в рецепт, ни закуской или гарниром к нему" in user
    other = build_dish_detail_messages("Котлеты по-киевски", 4, dish={"uses": []},
                                       leftovers=LEFT)[1]["content"]
    assert "пристроены в другие блюда плана — сюда специально не добавляй" in other
    plain = build_dish_detail_messages("Борщ", 4, dish={})[1]["content"]
    assert "Остатки" not in plain


def test_free_leftovers_and_status():
    dishes = [{"name": "Пот-пай", "uses": ["Сельдерей", "морковь"]}, {"name": "Котлеты"}]
    assert free_leftovers(["сельдерей", "морковь", "оливки"], dishes) == ["оливки"]
    st = leftovers_status(["сельдерей", "морковь", "оливки"], dishes)
    assert "«Пот-пай» — Сельдерей, морковь" in st and "Не пристроены: оливки" in st
    assert leftovers_status([], dishes) == ""


def test_single_dish_gets_free_leftovers():
    user = build_single_dish_messages("", plan_dishes=[{"name": "Борщ"}],
                                      leftovers=["оливки"])[1]["content"]
    assert "Остатки пользователя, ещё не пристроенные в план: оливки" in user


# --- разбор ответа модели ----------------------------------------------------------------------

def test_stream_parser_extracts_leftovers():
    p = PlanStreamParser()
    p.feed('{"reply": "ок", "title": "Неделя", "leftovers": ["порей", "солёные огурцы"], '
           '"dishes": [{"name": "Рассольник", "uses": ["солёные огурцы"]}]}')
    assert p.leftovers() == ["порей", "солёные огурцы"]
    assert p.new_dishes()[0]["uses"] == ["солёные огурцы"]
    empty = PlanStreamParser()
    empty.feed('{"reply": "ок", "title": "Неделя", "dishes": []}')
    assert empty.leftovers() == []


def test_generate_plan_returns_clean_leftovers_and_uses(monkeypatch):
    gate = FakeGate({
        "reply": "ок", "title": "Неделя",
        "leftovers": ["порей", " Порей ", "", "сельдерей"],
        "dishes": [{"name": "Курица с пореем", "emoji": "🍗", "uses": ["порей", "порей"]},
                   {"name": "Котлеты по-киевски", "emoji": "🍗"}],
    })
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    data = asyncio.run(planner.generate_plan("меню, остались порей и сельдерей", [], 2,
                                             count_plan=False, variety="", book=["Борщ"]))
    assert data["leftovers"] == ["порей", "сельдерей"]
    assert data["dishes"][0]["uses"] == ["порей"] and data["dishes"][1]["uses"] == []
    assert "Книга рецептов семьи: " not in gate.calls[0][0][0]["content"]  # книга — в user
    assert "Борщ" in gate.calls[0][0][1]["content"]


def test_replace_offers_only_unplaced_leftovers(monkeypatch):
    gate = FakeGate({"reply": "ок", "dish": {"name": "Курица по-провански", "emoji": "🍗",
                                            "uses": ["оливки"]}})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    plan = [{"id": "a", "name": "Пот-пай", "uses": ["сельдерей"]},
            {"id": "b", "name": "Гуляш", "uses": ["оливки"]}]
    res = asyncio.run(planner.replace_dish_by_id(plan, "План", "b", "", leftovers=["сельдерей", "оливки"]))
    user = gate.calls[0][0][1]["content"]
    assert "ещё не пристроенные в план: оливки" in user  # сельдерей уже в пот-пае
    assert res["dishes"][1]["uses"] == ["оливки"]


# --- покупки: «Есть дома» ---------------------------------------------------------------------

def test_is_leftover_matches_forms_but_not_derived_products():
    assert is_leftover("Лук репчатый", LEFT)
    assert is_leftover("Огурцы солёные", LEFT)
    assert is_leftover("Корень имбиря", LEFT)
    assert is_leftover("Стебли сельдерея", LEFT)
    assert is_leftover("Лук-порей", LEFT)
    assert not is_leftover("Оливковое масло", LEFT)
    assert not is_leftover("Имбирь молотый", LEFT)
    assert not is_leftover("Лук-порей", ["лук"])  # порей — не репчатый лук
    assert not is_leftover("Томатная паста", ["томаты"])
    assert not is_leftover("Чеснок", LEFT)
    assert not is_leftover("Морковь", [])


def test_group_items_moves_leftovers_to_home_group_last():
    items = [
        {"name": "морковь", "qty": 300, "unit": "г", "category": "Овощи"},
        {"name": "картофель", "qty": 500, "unit": "г", "category": "Овощи"},
        {"name": "оливковое масло", "qty": 30, "unit": "мл", "category": "Прочее"},
        {"name": "курица", "qty": 1, "unit": "кг", "category": "Мясо и птица"},
    ]
    groups = group_items(items, LEFT)
    assert groups[-1].category == HOME_CATEGORY
    assert [i.name for i in groups[-1].items] == ["морковь"]
    assert [g.category for g in group_items(items)][-1] == "Прочее"  # без остатков — как было


# --- книга рецептов ----------------------------------------------------------------------------

def _book_dish(name, **kw):
    return {"id": "x", "name": name, "emoji": "🥧", "servings": 6, "prep_min": 25, "cook_min": 40,
            "storage": {"vacuum": True, "freeze": True, "shelf_life_days": 60, "note": "разморозь"},
            "ingredients": [{"name": "курица", "qty": 800, "unit": "г", "category": "Мясо и птица"}],
            "steps": ["шаг 1"], "tips": [], "detail_provider": "Claude",
            "variants": {"anthropic": {"ingredients": [], "steps": ["шаг 1"]}},
            "active_model": "anthropic", **kw}


def _seed_book(session):
    session.add(Conversation(id="c"))
    session.add(PlanRow(id="old", conversation_id="c", title="t", week_label="w", status="accepted",
                        dishes=[_book_dish("Начинка для чикен пот пай"),
                                {"id": "y", "name": "Борщ", "emoji": "🍲"}]))  # без рецепта
    session.add(PlanRow(id="draft", conversation_id="c", title="t", week_label="w",
                        dishes=[_book_dish("Солянка")]))  # черновик — не книга
    session.add(FavoriteRecipe(key="начинка для чикен пот пай", name="Начинка для чикен пот пай"))
    session.commit()


def test_book_names_only_accepted_with_recipe(session):
    _seed_book(session)
    assert recipebook.book_names(session) == ["Начинка для чикен пот пай"]


def test_attach_copies_book_recipe_keeps_plan_fields(session):
    _seed_book(session)
    index = recipebook.book_index(session)
    new = {"id": "dish-1", "name": "Начинка для  чикен пот ПАЙ", "emoji": "🥘", "servings": 4,
           "uses": ["сельдерей"], "ingredients": [], "steps": []}
    got = recipebook.attach(new, index)
    assert got["from_book"] and got["steps"] == ["шаг 1"] and got["servings"] == 6
    assert got["active_model"] == "anthropic" and "anthropic" in got["variants"]
    assert got["id"] == "dish-1" and got["emoji"] == "🥘" and got["uses"] == ["сельдерей"]
    declined = recipebook.attach({"id": "d3", "name": "Начинка для чикен пот пая"}, index)
    assert declined["from_book"] and declined["name"] == "Начинка для чикен пот пай"
    longer = {"id": "d4", "name": "Начинка для чикен пот пай с грибами"}
    assert recipebook.attach(longer, index) is longer  # другое блюдо — больше слов
    other = {"id": "d2", "name": "Котлеты по-киевски", "steps": []}
    assert recipebook.attach(other, index) is other
    has_recipe = {**new, "steps": ["свой"]}
    assert recipebook.attach(has_recipe, index) is has_recipe  # готовый рецепт не трогаем


def test_sync_uses_follows_recipe_ingredients():
    """План (Haiku) записал оливки и огурцы «закуской» к котлетам по-киевски, рецепт их не взял —
    uses берём из ингредиентов рецепта; без рецепта — намерение плана как есть."""
    ing = lambda n: {"name": n, "qty": 1, "unit": "г", "category": "Прочее"}  # noqa: E731
    kiev = {"name": "Котлеты по-киевски", "uses": ["оливки", "солёные огурцы"],
            "ingredients": [ing("куриное филе"), ing("сливочное масло"), ing("оливковое масло")]}
    assert sync_uses(kiev, LEFT)["uses"] == []
    soup = {"name": "Суп", "uses": ["порей"],
            "ingredients": [ing("лук-порей"), ing("морковь"), ing("корень сельдерея")]}
    assert sync_uses(soup, LEFT)["uses"] == ["порей", "сельдерей", "морковь"]
    draft = {"name": "Рассольник", "uses": ["солёные огурцы"], "ingredients": []}
    assert sync_uses(draft, LEFT) is draft
    assert sync_uses(kiev, []) is kiev


def test_detail_drops_leftover_promises_from_plan_reply():
    """Реплика плана «оливки с солёными огурцами — в закуску к котлетам» с «выполни» перебивала
    правило остатков — предложения про остатки вырезаем, остальные обещания оставляем."""
    reply = ("Отлично! Включу остатки в меню: порей и сельдерей пойдут в суп, оливки с солёными "
             "огурцами — в закуску к котлетам. Котлеты по-киевски — в панировке с горчицей.")
    user = build_dish_detail_messages("Котлеты по-киевски", 6, dish={"uses": []}, mention=reply,
                                      leftovers=LEFT)[1]["content"]
    assert "оливки" not in user.split("Остатки из запроса")[0]
    assert "в панировке с горчицей" in user
    plain = build_dish_detail_messages("Котлеты по-киевски", 6, dish={}, mention=reply)[1]["content"]
    assert "закуску к котлетам" in plain  # без остатков в плане — реплика как была


def test_desc_in_plan_prompt_and_recipe_header(monkeypatch):
    """Задумка блюда (desc) из плана → в промпт рецепта; без подачи/закусок."""
    assert "desc — 1–2 коротких предложения" in DEEPSEEK_PLAN_SYSTEM
    assert "без подачи, гарниров, закусок" in DEEPSEEK_PLAN_SYSTEM
    dish = {"desc": "Курица, тушённая с оливками и лимоном в духовке.", "tags": ["курица"]}
    user = build_dish_detail_messages("Курица по-провански", 4, dish=dish)[1]["content"]
    assert "Задумка блюда из плана (рецепт ей соответствует): Курица, тушённая" in user
    gate = FakeGate({"reply": "ок", "title": "Неделя", "dishes": [
        {"name": "Курица по-провански", "emoji": "🍗", "desc": "  Курица   с оливками. " * 30}]})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    d = asyncio.run(planner.generate_plan("меню", [], 1, count_plan=False, variety=""))["dishes"][0]
    assert d["desc"].startswith("Курица с оливками.") and len(d["desc"]) <= 300


def test_garnish_with_leftovers_not_in_recipe_header():
    """План до фикса записал котлетам гарнир «салат из оливок и солёных огурцов» — после
    перегенерации шаги и note всё равно подавали салат. Гарнир с остатками в шапку не берём."""
    kiev = {"garnish": "салат из оливок и солёных огурцов", "uses": [], "prep_min": 40}
    user = build_dish_detail_messages("Котлеты по-киевски", 6, dish=kiev, leftovers=LEFT)[1]["content"]
    assert "гарнир" not in user and "салат из оливок" not in user
    rice = build_dish_detail_messages("Рагу", 6, dish={"garnish": "рис"}, leftovers=LEFT)[1]["content"]
    assert "гарнир: рис" in rice
    plain = build_dish_detail_messages("Котлеты", 6, dish=kiev)[1]["content"]
    assert "гарнир: салат из оливок" in plain  # без остатков в плане — как было
