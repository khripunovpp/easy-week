"""Сводка беседы: дебаунс, старт без сводки, одна перезаписываемая сводка, модель задачи
summary, память (первое сообщение + сводка) в промптах чата."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.ai import planner
from app.ai.prompt import SUMMARY_SCHEMA, build_ds_plan_messages, chat_memory_block
from app.config import settings as config
from app.models import Conversation, MessageRow, PlanRow
from app.routers import chat as chat_router
from app.services import summary

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class FakeGate:
    provider = "Fake"
    key = "fake"

    def __init__(self, text="- хочет ужины без рыбы"):
        self.text = text
        self.calls = []

    async def complete_json(self, messages, **kw):
        self.calls.append((messages, kw))
        return {"summary": self.text}, {}


def _msg(session, conv, i, role, text, **kw):
    m = MessageRow(id=f"m{i}", conversation_id=conv, role=role, text=text,
                   created_at=T0 + timedelta(seconds=i), **kw)
    session.add(m)
    session.commit()
    return m


@pytest.fixture()
def conv(session):
    session.add(Conversation(id="c1"))
    session.commit()
    return "c1"


def test_no_summary_at_start(session, conv, monkeypatch):
    gate = FakeGate()
    monkeypatch.setattr(summary, "gate_for", lambda m, task="chat": gate)
    _msg(session, conv, 1, "user", "5 ужинов на неделю")
    _msg(session, conv, 2, "assistant", "Готово — вот план")
    assert asyncio.run(summary.summarize(conv, session)) is None
    assert gate.calls == [] and session.get(Conversation, conv).summary is None


def test_summary_incremental_and_single(session, conv, monkeypatch):
    gate = FakeGate("- 5 ужинов\n- без рыбы")
    seen = []
    monkeypatch.setattr(summary, "gate_for", lambda m, task="chat": seen.append(task) or gate)
    session.add(PlanRow(id="p1", conversation_id=conv, title="План", week_label="1–7",
                        dishes=[{"name": "Борщ"}, {"name": "Плов"}]))
    session.commit()
    _msg(session, conv, 1, "user", "5 ужинов на неделю")
    _msg(session, conv, 2, "assistant", "Готово", plan_id="p1")
    _msg(session, conv, 3, "user", "убери рыбу", discuss_target=None)
    out = asyncio.run(summary.summarize(conv, session))
    assert out == "- 5 ужинов\n- без рыбы" and seen == ["summary"]
    user_prompt = gate.calls[0][0][1]["content"]
    assert "Прошлая сводка" not in user_prompt  # первой сводке нечего продолжать
    # Состав плана в сводку не идёт (модель пересказывала его неверно), реплики — идут.
    assert "Борщ" not in user_prompt and "Пользователь: убери рыбу" in user_prompt
    assert "Ассистент: Готово" in user_prompt
    c = session.get(Conversation, conv)
    assert c.summary == out and c.summary_upto == "m3"

    # Без новых реплик пользователя — не пересчитываем.
    _msg(session, conv, 4, "assistant", "Убрала")
    assert asyncio.run(summary.summarize(conv, session)) is None and len(gate.calls) == 1

    # Новая реплика → в промпт идёт прошлая сводка + только новое; поле перезаписано.
    gate.text = "- 5 ужинов, без рыбы\n- добавить суп"
    _msg(session, conv, 5, "user", "добавь суп")
    assert asyncio.run(summary.summarize(conv, session)) == gate.text
    p2 = gate.calls[1][0][1]["content"]
    assert "Прошлая сводка:\n- 5 ужинов\n- без рыбы" in p2
    assert "убери рыбу" not in p2 and "Ассистент: Убрала" in p2 and "добавь суп" in p2
    c = session.get(Conversation, conv)
    assert c.summary == gate.text and c.summary_upto == "m5"


def test_cloudflare_gets_schema(session, conv, monkeypatch):
    gate = FakeGate()
    monkeypatch.setattr(summary, "cloudflare", gate)
    monkeypatch.setattr(summary, "gate_for", lambda m, task="chat": gate)
    _msg(session, conv, 1, "user", "план")
    _msg(session, conv, 2, "user", "без свинины")
    asyncio.run(summary.summarize(conv, session))
    kw = gate.calls[0][1]
    assert kw["schema"] is SUMMARY_SCHEMA and kw["model"] == config.cf_model_judge


def test_empty_summary_is_error(session, conv, monkeypatch):
    monkeypatch.setattr(summary, "gate_for", lambda m, task="chat": FakeGate(""))
    _msg(session, conv, 1, "user", "план")
    _msg(session, conv, 2, "user", "ещё")
    with pytest.raises(summary.AIError):
        asyncio.run(summary.summarize(conv, session))
    assert session.get(Conversation, conv).summary is None


def test_debounce_coalesces_series(monkeypatch):
    calls = []

    async def fake_summarize(conv_id, session=None):
        calls.append(conv_id)

    monkeypatch.setattr(summary, "DEBOUNCE_SEC", 0.05)
    monkeypatch.setattr(summary, "summarize", fake_summarize)

    async def run():
        summary.schedule("c1")
        await asyncio.sleep(0.02)
        summary.schedule("c1")  # написал ещё раз раньше 5 с — таймер перезапускается
        await asyncio.sleep(0.02)
        summary.schedule("c1")
        summary.schedule("c2")
        await asyncio.sleep(0.03)
        assert calls == []  # ещё тишина не настала
        await asyncio.sleep(0.08)

    asyncio.run(run())
    assert sorted(calls) == ["c1", "c2"]  # по одному вызову на беседу


def test_memory_first_message_and_summary(session, conv):
    _msg(session, conv, 1, "user", "5 ужинов без рыбы")
    # Старт чата: текущая реплика и есть первое сообщение — не дублируем, сводки нет.
    assert summary.memory(session, conv, "5 ужинов без рыбы") == ""
    _msg(session, conv, 2, "user", "добавь суп")
    mem = summary.memory(session, conv, "добавь суп")
    assert "Первое сообщение пользователя (с чего начался чат): 5 ужинов без рыбы" in mem
    assert "Сводка беседы" not in mem
    c = session.get(Conversation, conv)
    c.summary = "- без рыбы"
    session.add(c)
    session.commit()
    mem = summary.memory(session, conv, "добавь суп")
    assert "Сводка беседы" in mem and "- без рыбы" in mem
    # И в промпт плана память попадает целиком.
    content = build_ds_plan_messages("добавь суп", [], 5, context=mem)[1]["content"]
    assert "5 ужинов без рыбы" in content and "- без рыбы" in content


def test_edit_context_has_first_message_summary_and_recent(session, conv):
    _msg(session, conv, 1, "user", "5 ужинов")
    _msg(session, conv, 2, "assistant", "Готово")
    _msg(session, conv, 3, "user", "убери суп")
    c = session.get(Conversation, conv)
    c.summary = "- 5 ужинов, убрать суп"
    session.add(c)
    session.commit()
    ctx = chat_router._edit_context(session, conv, "убери суп")
    assert "Первое сообщение пользователя (с чего начался чат): 5 ужинов" in ctx
    assert "- 5 ужинов, убрать суп" in ctx and "Ты: Готово" in ctx
    assert "Пользователь: убери суп" not in ctx  # текущая реплика — «Просьба», не контекст


def test_plan_stream_passes_context(monkeypatch):
    class StreamGate:
        provider = "Fake"
        key = "fake"
        supports_stream = True

        def __init__(self):
            self.messages = None

        async def stream_json(self, messages, **kw):
            self.messages = messages
            yield '{"reply":"ок","title":"План","dishes":[{"name":"Борщ","emoji":"🍲"}]}'

    gate = StreamGate()
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)

    async def run():
        return [ev async for ev in planner.generate_plan_stream(
            "ещё план", [], 3, context=chat_memory_block("5 ужинов", "- без рыбы"))]

    events = asyncio.run(run())
    assert any(k == "dish" for k, _ in events)
    user = gate.messages[1]["content"]
    assert "Первое сообщение пользователя (с чего начался чат): 5 ужинов" in user
    assert "- без рыбы" in user


def test_plan_operations_not_in_summary_prompt(session, conv, monkeypatch):
    # Кнопки и «Готово: …» — операции над планом: модель пересказывала их неверным состоянием.
    gate = FakeGate("- славянская кухня")
    monkeypatch.setattr(summary, "gate_for", lambda m, task="chat": gate)
    _msg(session, conv, 1, "user", "хочу славянскую кухню")
    _msg(session, conv, 2, "assistant", "Вот план")
    _msg(session, conv, 3, "user", "Добавить блюдо: жаркое, салат крабовый")
    _msg(session, conv, 4, "assistant", "Готово: добавлено «Жаркое».")
    _msg(session, conv, 5, "user", "Замена «Щи»: что-то полегче")
    _msg(session, conv, 6, "user", "не люблю щи")
    asyncio.run(summary.summarize(conv, session))
    user_prompt = gate.calls[0][0][1]["content"]
    assert "Пользователь: не люблю щи" in user_prompt and "Ассистент: Вот план" in user_prompt
    assert "Добавить блюдо" not in user_prompt and "Готово" not in user_prompt
    assert "Замена" not in user_prompt
