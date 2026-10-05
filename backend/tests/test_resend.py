"""«↻ Переотправить» в чате: повтор упавшего запроса не дублирует реплику пользователя в истории,
у нового чата, упавшего до meta, фронт узнаёт беседу из события ошибки."""

import asyncio

from sqlmodel import select

from app.ai import limits
from app.ai.base import AIError
from app.models import Conversation, MessageRow
from app.routers import chat as chat_router
from app.routers import discuss as discuss_router
from app.schemas import ChatRequest, DiscussRequest
from app.services.history import is_repeat


def _quiet(monkeypatch):
    monkeypatch.setattr(chat_router.prefs, "learn_async", lambda *a, **kw: None)
    monkeypatch.setattr(chat_router.chat_summary, "schedule", lambda *a, **kw: None)
    monkeypatch.setattr(discuss_router.chat_summary, "schedule", lambda *a, **kw: None)


def _user_texts(session, conv_id):
    rows = session.exec(
        select(MessageRow).where(MessageRow.conversation_id == conv_id, MessageRow.role == "user")
    ).all()
    return [m.text for m in rows]


def _stream(session, req):
    async def run():
        return [ev async for ev in chat_router.chat_stream(req, session)]
    return asyncio.run(run())


def test_stream_resend_keeps_conversation_and_one_user_message(monkeypatch, session):
    _quiet(monkeypatch)

    async def failing(*a, **kw):
        raise AIError("Дневной лимит Claude исчерпан: 2 плана в день.")
        yield  # pragma: no cover — делает функцию генератором

    monkeypatch.setattr(chat_router, "generate_plan_stream", failing)
    events = _stream(session, ChatRequest(message="что скажешь", recipe_model="deepseek"))
    err = events[-1]
    assert err.event == "error" and "лимит" in err.data["message"]
    conv_id = err.data["conversationId"]  # повтор пойдёт в эту же беседу
    assert _user_texts(session, conv_id) == ["что скажешь"]

    async def ok(*a, **kw):
        yield "meta", {"title": "План", "week_label": "w", "reply": "Готово", "provider": "Fake"}
        yield "dish", {"id": "d", "name": "Щи", "emoji": "🍲", "servings": 4, "prep_min": 1,
                       "cook_min": 1, "storage": {"shelf_life_days": 30}}
        yield "leftovers", []

    monkeypatch.setattr(chat_router, "generate_plan_stream", ok)
    events = _stream(session, ChatRequest(conversation_id=conv_id, message="что скажешь",
                                          recipe_model="deepseek", resend=True))
    assert events[-1].event == "done"
    assert _user_texts(session, conv_id) == ["что скажешь"]  # без дубля
    # обычная отправка того же текста (не повтор) — пишется как новая реплика
    _stream(session, ChatRequest(conversation_id=conv_id, message="что скажешь",
                                 recipe_model="deepseek"))
    assert _user_texts(session, conv_id) == ["что скажешь", "что скажешь"]


def test_is_repeat_only_when_last_message_is_same_user_text(session):
    session.add(Conversation(id="c"))
    session.add(MessageRow(id="1", conversation_id="c", role="user", text="ужины"))
    session.commit()
    assert is_repeat(session, "c", " ужины ") and not is_repeat(session, "c", "обеды")
    session.add(MessageRow(id="2", conversation_id="c", role="assistant", text="готово"))
    session.commit()
    assert not is_repeat(session, "c", "ужины")  # ответ был — это новая реплика
    assert not is_repeat(session, None, "ужины")


def test_discuss_resend_does_not_duplicate_question(monkeypatch, session):
    from tests.test_discuss import FakeGate, _seed, _use

    _quiet(monkeypatch)
    _seed(session)
    _use(monkeypatch, FakeGate(fail=True))
    req = DiscussRequest(plan_id="p1", target="recipe", dish_id="gulyash",
                         message="Можно без свинины?", recipe_model="fake")
    try:
        asyncio.run(discuss_router.chat_discuss(req, session))
    except Exception:  # noqa: BLE001 — упавший ответ (502)
        pass
    gate = FakeGate([{"reply": "Да.", "action": {"op": "none"}}])
    _use(monkeypatch, gate)
    asyncio.run(discuss_router.chat_discuss(req.model_copy(update={"resend": True}), session))
    texts = [m.text for m in session.exec(
        select(MessageRow).where(MessageRow.discuss_target == "recipe", MessageRow.role == "user")
    ).all()]
    assert texts == ["Можно без свинины?"]
    sent = "\n".join(m["content"] for m in gate.calls[0])
    assert sent.count("Можно без свинины?") == 1  # вопрос — один раз, не прошлой репликой


def test_without_last_question_trims_only_tail():
    turns = [{"role": "user", "content": "раз\n\nМожно без свинины?"}]
    assert discuss_router._without_last_question(turns, "Можно без свинины?") == [
        {"role": "user", "content": "раз"}]
    assert discuss_router._without_last_question(
        [{"role": "user", "content": "Можно без свинины?"}], "Можно без свинины?") == []
    other = [{"role": "assistant", "content": "Можно без свинины?"}]
    assert discuss_router._without_last_question(other, "Можно без свинины?") == other


def test_limit_text_plural():
    assert limits._count_word(1, "plan") == "1 план"
    assert limits._count_word(2, "plan") == "2 плана"
    assert limits._count_word(5, "plan") == "5 планов"
    assert limits._count_word(11, "recipe") == "11 рецептов"
    assert limits._count_word(22, "recipe") == "22 рецепта"
