"""Реплика и остатки плана — только по словам пользователя. 👎 2026-10-05: на запрос «В день
подачи» Claude Haiku написала «плов вместо надоевшей терияки, рыба без картошки, остатки творога
и колбасок пристроила» — взяла это из «недавно ели», книги рецептов и правил заготовки."""

import asyncio

from app.ai import planner
from app.ai.prompt import (
    DEEPSEEK_PLAN_SYSTEM,
    NAMES_SYSTEM,
    SERVICE_HEADER,
    SINGLE_DISH_SYSTEM,
    build_ds_plan_messages,
    build_single_dish_messages,
)

HAIKU_REPLY = (
    "Пять блюд на неделю с осенним уклоном — всё под заморозку и подачу в день. Узбекский плов "
    "вместо надоевшей терияки, рыба в сливках без картошки. Остатки творога и колбасок "
    "пристроила, где они уместны."
)


def _plan(leftovers, uses):
    return {"reply": HAIKU_REPLY, "title": "Осенний запас", "leftovers": leftovers,
            "dishes": [{"name": "Запеканка творожная", "uses": uses}]}


def test_invented_leftovers_dropped_with_reply_sentence():
    got = planner._ground_plan(_plan(["творог", "охотничьи колбаски"], ["творог"]), "В день подачи")
    assert got["leftovers"] == [] and got["dishes"][0]["uses"] == []
    assert "творог" not in got["reply"].lower() and got["reply"].startswith("Пять блюд")


def test_named_leftovers_kept_in_any_word_form():
    text = "Ужины на неделю, остался творог и пара перцев, колбаски надо пристроить"
    got = planner._ground_plan(_plan(["творог", "перец", "охотничьи колбаски"], ["творог"]), text)
    assert got["leftovers"] == ["творог", "перец", "охотничьи колбаски"]
    assert got["dishes"][0]["uses"] == ["творог"] and got["reply"] == HAIKU_REPLY


def test_known_leftovers_of_plan_stay_grounded():
    # правка/пересборка: остатки плана уже известны — их пользователь назвал раньше
    ground = planner._ground_text("сделай заново", None, ["сельдерей"])
    got = planner._ground_plan(_plan(["сельдерей"], ["сельдерей"]), ground)
    assert got["leftovers"] == ["сельдерей"]


def test_stream_drops_invented_leftovers_and_fixes_reply(monkeypatch):
    import json

    payload = json.dumps({
        "reply": HAIKU_REPLY, "title": "Осенний запас", "leftovers": ["творог", "колбаски"],
        "dishes": [{"name": "Запеканка творожная", "emoji": "🍰", "uses": ["творог"]}],
    }, ensure_ascii=False)

    class StreamGate:
        provider = "Fake"
        key = "fake"
        supports_stream = True

        async def stream_json(self, messages, **kw):
            for i in range(0, len(payload), 40):
                yield payload[i:i + 40]

    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": StreamGate())

    async def run():
        return [ev async for ev in planner.generate_plan_stream(
            "В день подачи", [], 5, user_text="В день подачи")]

    events = dict(asyncio.run(run()))
    assert events["dish"]["uses"] == [] and events["leftovers"] == []
    assert "творог" not in events["reply"].lower()
    assert events["meta"]["reply"] == HAIKU_REPLY  # meta ушла раньше — правка отдельным событием


def test_single_dish_uses_only_given_leftovers(monkeypatch):
    class Gate:
        provider = "Fake"
        key = "fake"

        async def complete_json(self, messages, **kw):
            return {"reply": "ок", "dish": {"name": "Сырники", "emoji": "🥞",
                                            "uses": ["творог", "колбаски"]}}, {}

    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": Gate())
    got = asyncio.run(planner.generate_single_dish("сырники", leftovers=["творог"]))
    assert got["dish"]["uses"] == ["творог"]
    got = asyncio.run(planner.generate_single_dish("сырники"))
    assert got["dish"]["uses"] == []


def test_prompts_separate_user_words_from_service_blocks():
    for system in (DEEPSEEK_PLAN_SYSTEM, NAMES_SYSTEM, SINGLE_DISH_SYSTEM):
        assert "не приписывай ему" in system
    user = build_ds_plan_messages(
        "В день подачи", ["Курица терияки"], 5, context="Первое сообщение пользователя: ужины",
        book=["Солянка с охотничьими колбасками"],
    )[1]["content"]
    head, service = user.split(SERVICE_HEADER)
    assert "В день подачи" in head and "Первое сообщение пользователя" in head
    assert "Курица терияки" in service and "Солянка" in service and "Количество блюд" in service
    single = build_single_dish_messages("салат на сегодня", avoid_titles=["Борщ"])[1]["content"]
    head, service = single.split(SERVICE_HEADER)
    assert "салат на сегодня" in head and "Борщ" in service
