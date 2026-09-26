"""Обсуждение в чате и «↻ Перегенерировать»: роутинг действий, запись вариантов только после
успеха, правка рецепта в чате (edit_dish) сохраняется в variants."""

import asyncio

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.ai import planner
from app.ai.base import AIError
from app.ai.prompt import DISH_DETAIL_SYSTEM
from app.models import Conversation, MessageRow, PlanRow
from app.routers import discuss as discuss_router
from app.routers import plans as plans_router
from app.schemas import DetailRequest, DiscussRequest
from app.services import discussion, regenerate

DETAIL = {
    "ingredients": [{"name": "говядина", "qty": 500, "unit": "г", "category": "Мясо и птица"}],
    "steps": ["Нарежь", "Туши"],
    "tips": ["Остуди"],
    "note": "Разогрей",
}


class FakeGate:
    """Гейт-заглушка: JSON-ответы по очереди (или AIError), tool-вызовы — заранее заданные."""

    provider = "Fake"
    key = "fake"
    supports_stream = False

    def __init__(self, parsed=None, *, tools=None, fail=False):
        self.parsed = list(parsed or [])
        self.tools = tools
        self.supports_tools = tools is not None
        self.fail = fail
        self.calls = []

    async def complete_json(self, messages, **kw):
        self.calls.append(messages)
        if self.fail:
            raise AIError("Fake не ответил")
        return self.parsed.pop(0), {}

    async def call_tools(self, messages, tools, **kw):
        self.calls.append(messages)
        return self.tools, ""


def _use(monkeypatch, gate):
    """Все точки выбора гейта → заглушка (планер, сервис перегенерации, роутеры)."""
    for mod in (planner, regenerate, plans_router):
        monkeypatch.setattr(mod, "gate_for", lambda m, task="chat", g=gate: g)
    # Роутер обсуждения сам гейт не берёт — только резолвит ключ модели реплики.
    monkeypatch.setattr(discuss_router, "resolve_key", lambda m, task="chat", g=gate: g.key)


def _seed(session, *, variants=True):
    session.add(Conversation(id="c1"))
    dish = {
        "id": "gulyash", "name": "Гуляш", "emoji": "🍲", "servings": 4, "prep_min": 10,
        "cook_min": 60, "tags": ["говядина"],
        "storage": {"vacuum": True, "freeze": True, "shelf_life_days": 60, "note": "старое"},
        "ingredients": [{"name": "свинина", "qty": 400, "unit": "г", "category": "Мясо и птица"}],
        "steps": ["старый шаг"], "tips": [], "detail_provider": "DeepSeek",
    }
    if variants:
        dish["variants"] = {"deepseek": {
            "ingredients": dish["ingredients"], "steps": dish["steps"], "tips": [],
            "note": "старое", "provider": "DeepSeek",
        }}
        dish["active_model"] = "deepseek"
    session.add(PlanRow(id="p1", conversation_id="c1", title="План", week_label="1–7",
                        dishes=[dish, {"id": "borsch", "name": "Борщ", "emoji": "🥣",
                                       "servings": 4, "prep_min": 5, "cook_min": 40,
                                       "storage": {"shelf_life_days": 60}}]))
    session.add(MessageRow(id="m0", conversation_id="c1", role="user", text="Мясное на неделю"))
    session.commit()


def _discuss(session, target="recipe", message="Можно без свинины?", dish_id="gulyash"):
    req = DiscussRequest(plan_id="p1", target=target, dish_id=dish_id, message=message,
                         recipe_model="fake")
    return asyncio.run(discuss_router.chat_discuss(req, session))


def test_discuss_answer_only_saves_tagged_messages_no_versions(session, monkeypatch):
    _seed(session)
    gate = FakeGate([{"reply": "Да, замени на говядину.", "action": {"op": "none"}}])
    _use(monkeypatch, gate)
    res = _discuss(session)
    assert res.op == "none" and res.dish is None and "говядину" in res.reply
    msgs = session.exec(select(MessageRow).where(MessageRow.discuss_target == "recipe")).all()
    assert [(m.role, m.dish_id) for m in msgs] == [("user", "gulyash"), ("assistant", "gulyash")]
    assert len(session.exec(select(PlanRow)).all()) == 1  # версий плана не создаём
    # Контекст: полный рецепт + другие блюда + исходный запрос; вопрос — последним user.
    sent = gate.calls[0]
    assert "свинина 400 г" in sent[1]["content"] and "Борщ" in sent[1]["content"]
    assert "Мясное на неделю" in sent[1]["content"]
    assert sent[-1]["role"] == "user" and "без свинины" in sent[-1]["content"]


def test_discuss_multiturn_merges_prior_thread(session, monkeypatch):
    _seed(session)
    _use(monkeypatch, FakeGate([{"reply": "Ок.", "action": {"op": "none"}}] * 2))
    _discuss(session, message="Первый вопрос")
    gate = FakeGate([{"reply": "Ок2.", "action": {"op": "none"}}])
    _use(monkeypatch, gate)
    _discuss(session, message="Второй вопрос")
    roles = [m["role"] for m in gate.calls[0]]
    assert roles == ["system", "user", "assistant", "user"]  # чередование сохранено
    assert "Первый вопрос" in gate.calls[0][1]["content"]  # склеено с контекстом


def test_discuss_edit_updates_variant_in_place(session, monkeypatch):
    _seed(session)
    gate = FakeGate([DETAIL], tools=[{"name": "update_recipe", "args": {"change": "говядина"}}])
    _use(monkeypatch, gate)
    res = _discuss(session, message="Сделай на говядине")
    assert res.op == "edit" and res.dish is not None
    assert res.dish.active_model == "fake" and res.dish.ingredients[0].name == "говядина"
    row = session.get(PlanRow, "p1")
    d = row.dishes[0]
    assert set(d["variants"]) == {"deepseek", "fake"} and d["active_model"] == "fake"
    assert len(session.exec(select(PlanRow)).all()) == 1
    # Перегенерация детали видит правку и обсуждение (в USER, system стабилен).
    detail_msgs = gate.calls[1]
    assert detail_msgs[0]["content"] == DISH_DETAIL_SYSTEM
    assert "говядина" in detail_msgs[1]["content"] and "Сделай на говядине" in detail_msgs[1]["content"]


def test_discuss_replace_suggests_and_disallowed_op_ignored(session, monkeypatch):
    _seed(session)
    _use(monkeypatch, FakeGate([{"reply": "Могу заменить.", "action": {"op": "replace", "query": "рыба"}}]))
    res = _discuss(session, message="Замени на рыбу")
    assert res.suggest_replace and res.replace_query == "рыба" and res.op == "replace"
    # Для плана готовки «edit» недопустим → none (ничего не меняем).
    _use(monkeypatch, FakeGate([{"reply": "Ок", "action": {"op": "edit", "change": "x"}}]))
    res2 = _discuss(session, target="cooking", message="Что сначала?", dish_id=None)
    assert res2.op == "none" and res2.cooking is None


def test_discuss_ai_error_is_502(session, monkeypatch):
    _seed(session)
    _use(monkeypatch, FakeGate(fail=True))
    with pytest.raises(HTTPException) as e:
        _discuss(session)
    assert e.value.status_code == 502 and "Обсуждение недоступно" in e.value.detail


def _regen(session):
    req = DetailRequest(recipe_model="fake", action="regenerate")
    return asyncio.run(plans_router.dish_details("p1", "gulyash", req, session))


def test_regenerate_writes_variant_only_on_success(session, monkeypatch):
    _seed(session)
    before = [dict(d) for d in session.get(PlanRow, "p1").dishes]
    _use(monkeypatch, FakeGate(fail=True))
    with pytest.raises(HTTPException) as e:
        _regen(session)
    assert e.value.status_code == 502
    session.expire_all()
    assert session.get(PlanRow, "p1").dishes == before  # старый вариант цел

    session.add(MessageRow(id="d1", conversation_id="c1", role="user", text="Хочу поострее",
                           discuss_target="recipe", dish_id="gulyash"))
    session.commit()
    gate = FakeGate([DETAIL])
    _use(monkeypatch, gate)
    dish = _regen(session)
    assert dish.active_model == "fake" and dish.steps == ["Нарежь", "Туши"]
    user = gate.calls[0][1]["content"]
    assert "Хочу поострее" in user and "ПЕРЕГЕНЕРАЦИЯ" in user and "свинина" in user
    # Повторная перегенерация той же моделью — снова генерим (не отдаём кэш варианта).
    gate2 = FakeGate([{**DETAIL, "steps": ["Новый шаг"]}])
    _use(monkeypatch, gate2)
    assert _regen(session).steps == ["Новый шаг"]


def test_edit_dish_in_chat_writes_variants(monkeypatch):
    dishes = [{"id": "g", "name": "Гуляш", "servings": 4, "storage": {"shelf_life_days": 60},
               "variants": {"deepseek": {"ingredients": [], "steps": ["старый"], "tips": [],
                                         "note": "", "provider": "DeepSeek"}},
               "active_model": "deepseek", "steps": ["старый"]}]
    gate = FakeGate([DETAIL], tools=[{"name": "edit_dish",
                                      "args": {"name": "Гуляш", "change": "без перца"}}])
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    res = asyncio.run(planner.edit_plan(dishes, "План", "Гуляш без перца"))
    d = res["dishes"][0]
    assert d["active_model"] == "fake" and d["variants"]["fake"]["steps"] == ["Нарежь", "Туши"]
    assert d["steps"] == ["Нарежь", "Туши"]  # плоские поля = активный вариант


def test_discussion_text_caps_and_falls_back_to_mentions(session):
    _seed(session)
    session.add(MessageRow(id="x1", conversation_id="c1", role="user", text="В гуляше меньше соли"))
    session.commit()
    txt = discussion.discussion_text(session, "c1", "recipe", "gulyash", "Гуляш",
                                     skip_first_user="Мясное на неделю")
    assert "меньше соли" in txt and "Мясное" not in txt
    for i in range(30):
        session.add(MessageRow(id=f"t{i}", conversation_id="c1", role="user", text="x" * 300,
                               discuss_target="cooking"))
    session.commit()
    capped = discussion.discussion_text(session, "c1", "cooking")
    assert len(capped.splitlines()) <= 12 and len(capped) <= 2500 + 12 * 20


def test_cooking_regenerate_uses_discussion_and_keeps_old_on_error(session, monkeypatch):
    _seed(session)
    session.add(MessageRow(id="k1", conversation_id="c1", role="user", text="Сначала бульон",
                           discuss_target="cooking"))
    session.commit()
    req = DetailRequest(recipe_model="fake", action="regenerate")
    cook = {"steps": [{"order": 1, "phase": "Готовка", "text": "Бульон", "activeMin": 5,
                       "passiveMin": 60, "dishes": ["Борщ"]}], "note": "итог"}
    gate = FakeGate([DETAIL, cook])  # деталь для Борща (бэкфилл) + план готовки
    _use(monkeypatch, gate)
    cp = asyncio.run(plans_router.cooking_plan("p1", req, session))
    assert cp.active_model == "fake" and cp.steps[0].text == "Бульон"
    assert "Сначала бульон" in gate.calls[-1][1]["content"]
    _use(monkeypatch, FakeGate(fail=True))
    with pytest.raises(HTTPException):
        asyncio.run(plans_router.cooking_plan("p1", req, session))
    session.expire_all()
    assert session.get(PlanRow, "p1").cooking_plan["variants"]["fake"]["note"] == "итог"
