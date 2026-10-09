"""«Исправить» в рецепте: модель отдаёт только изменения строк, код их применяет — остальное
дословно; новая версия в том же слоте (kind fix), сдвиг шагов моделью не применяется."""

import asyncio

import pytest
from fastapi import HTTPException

from app.ai import planner
from app.ai.base import AIError
from app.ai.prompt import FIX_RECIPE_SYSTEM, build_fix_messages
from app.models import Conversation, PlanRow
from app.routers import plans as plans_router
from app.schemas import FixRequest
from app.services import planstore
from app.services import settings as app_settings
from app.services.variants import with_detail

ING = [{"name": "Говяжий фарш", "qty": 400, "unit": "г", "category": "Мясо и птица"},
       {"name": "Лук репчатый", "qty": 100, "unit": "г", "category": "Овощи"},
       {"name": "Морковь", "qty": 150, "unit": "г", "category": "Овощи"}]
STEPS = ["Смешай фарш с мелко нарезанным луком, сформуй фрикадельки.",
         "Морковь нарежь кружками, вари в бульоне 5 минут.",
         "Опусти фрикадельки, вари 10 минут."]
DESC = "Бульон с морковью и фрикадельками из фарша с луком."


def _dish() -> dict:
    base = {"id": "soup", "name": "Суп", "emoji": "🍲", "servings": 4, "prep_min": 10,
            "cook_min": 30, "desc": "задумка плана",
            "storage": {"freeze": True, "shelf_life_days": 60, "note": ""}}
    det = {"ingredients": ING, "steps": STEPS, "tips": ["Не переваривай."], "note": "",
           "desc": DESC, "provider": "Claude", "model": "anthropic",
           "model_ref": "anthropic:claude-haiku-4-5", "gen_id": "g0"}
    return with_detail(base, "anthropic", det, kind="generate")


DIFF = {"ingredients": [{"i": 2, "remove": True}],
        "steps": [{"i": 1, "text": "Смешай фарш, сформуй фрикадельки."}],
        "desc": "Бульон с морковью и фрикадельками из говяжьего фарша.",
        "reply": "Убрал лук из ингредиентов, шага 1 и описания."}


class Gate:
    provider, key, configured = "DeepSeek", "deepseek", True

    def __init__(self, parsed):
        self.parsed, self.calls = parsed, []

    async def complete_json(self, messages, **kw):
        self.calls.append((messages, kw))
        return self.parsed, {}


def test_prompt_numbers_lines_and_asks_only_changes():
    msgs = build_fix_messages(_dish(), DESC, "убери лук")
    assert "Верни ТОЛЬКО изменения" in FIX_RECIPE_SYSTEM and msgs[0]["content"] == FIX_RECIPE_SYSTEM
    user = msgs[1]["content"]
    assert "[2] Лук репчатый — 100 г" in user and "[1] Смешай фарш" in user
    assert user.endswith("Просьба пользователя: убери лук")


def test_fix_task_models_exclude_cloudflare():
    assert "fix" in app_settings.TASKS and app_settings.builtin_defaults()["fix"] == "deepseek"
    assert not app_settings.allowed("fix", "cloudflare") and app_settings.allowed("fix", "deepseek")


def test_apply_fix_changes_only_listed_lines():
    recipe, n = planner.apply_fix(_dish(), DESC, DIFF)
    assert [i["name"] for i in recipe["ingredients"]] == ["Говяжий фарш", "Морковь"]
    assert recipe["steps"] == ["Смешай фарш, сформуй фрикадельки.", STEPS[1], STEPS[2]]
    assert recipe["tips"] == ["Не переваривай."] and recipe["desc"].endswith("говяжьего фарша.")
    assert n == 3
    # номер вне списка — пропуск; добавление и удаление шага
    recipe, n = planner.apply_fix(_dish(), DESC, {
        "ingredients": [{"i": 9, "remove": True},
                        {"add": True, "name": "Укроп", "qty": 10, "unit": "г", "category": "Овощи"}],
        "steps": [{"i": 3, "remove": True}]})
    assert recipe["ingredients"][-1]["name"] == "Укроп" and len(recipe["steps"]) == 2 and n == 2
    assert planner.apply_fix(_dish(), DESC, {"ingredients": [], "steps": []})[1] == 0


def test_apply_fix_rejects_shifted_step():
    """Cloudflare сдвинул номера: в шаг 1 записал текст шага 3 — правку не применяем."""
    with pytest.raises(AIError):
        planner.apply_fix(_dish(), DESC, {"steps": [{"i": 1, "text": STEPS[2]}]})


def _seed(session):
    session.add(Conversation(id="c"))
    planstore.new_row(session, id="p", conversation_id="c", title="П", week_label="w",
                      dishes=[_dish()])
    session.commit()


def test_fix_endpoint_writes_new_version_in_same_slot(session, monkeypatch):
    _seed(session)
    gate = Gate(DIFF)
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    out = asyncio.run(plans_router.dish_fix(
        "p", "soup", FixRequest(request="убери лук", recipe_model="deepseek"), session))
    assert out.reply.startswith("Убрал лук") and out.dish.desc.endswith("говяжьего фарша.")
    assert [i.name for i in out.dish.ingredients] == ["Говяжий фарш", "Морковь"]
    session.expire_all()
    d = session.get(PlanRow, "p").dishes[0]
    v = d["variants"]["anthropic"]
    assert d["active_model"] == "anthropic" and set(d["variants"]) == {"anthropic"}
    assert v["kind"] == "fix" and v["change"] == "убери лук" and v["parent_id"] == "anthropic"
    assert v["model_ref"] == "anthropic:claude-haiku-4-5" and v["fix_ref"].startswith("deepseek")
    assert v["provider"] == "Claude" and d["desc"] == "задумка плана"


def test_fix_nothing_is_422_and_writes_nothing(session, monkeypatch):
    _seed(session)
    gate = Gate({"ingredients": [], "steps": [], "reply": "Сельдерея в рецепте нет."})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    with pytest.raises(HTTPException) as e:
        asyncio.run(plans_router.dish_fix("p", "soup", FixRequest(request="убери сельдерей"),
                                          session))
    assert e.value.status_code == 422 and "Сельдерея" in e.value.detail
    session.expire_all()
    assert session.get(PlanRow, "p").dishes[0]["variants"]["anthropic"]["kind"] == "generate"
