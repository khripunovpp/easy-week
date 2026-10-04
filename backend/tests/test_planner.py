"""Планер: гарантия одного блюда при замене/добавлении, зерно разнообразия, промпты."""

import asyncio
import random

from app.ai import anthropic as claude
from app.ai import planner, prefs
from app.ai.prompt import (
    DEEPSEEK_PLAN_SYSTEM,
    NAMES_SYSTEM,
    build_dish_detail_messages,
    build_ds_plan_messages,
    build_single_dish_messages,
)


class FakeGate:
    """Гейт-заглушка: отдаёт заранее заданный ответ и запоминает сообщения."""

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


def _dish(name, tags=()):
    return {"name": name, "emoji": "🍲", "servings": 4, "prep_min": 10, "cook_min": 30,
            "tags": list(tags)}


PLAN = [
    {"id": "a", "name": "Борщ", "tags": ["суп"]},
    {"id": "b", "name": "Гуляш", "tags": ["говядина"]},
    {"id": "c", "name": "Плов", "tags": ["рис"]},
]


def test_replace_takes_exactly_one_even_if_model_returns_five(monkeypatch):
    many = [_dish(n) for n in ("Суп с фрикадельками", "Рыба запечённая", "Котлеты",
                               "Лосось в сливках", "Щи")]
    gate = FakeGate({"reply": "ок", "dishes": many})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    res = asyncio.run(planner.replace_dish_by_id(PLAN, "План", "b", "что-нибудь из рыбы"))
    assert len(res["dishes"]) == len(PLAN)  # никаких «5 блюд вместо одного»
    names = [d["name"] for d in res["dishes"]]
    assert names[0] == "Борщ" and names[2] == "Плов"
    assert "рыб" in names[1].lower()  # выбрано лучшее по пожеланию
    assert res["changed"] == [f"«Гуляш» заменено на «{names[1]}»"]


def test_replace_single_dish_object_and_context_in_prompt(monkeypatch):
    gate = FakeGate({"reply": "ок", "dish": _dish("Треска по-польски", ["рыба"])})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    res = asyncio.run(planner.replace_dish_by_id(
        PLAN, "План", "b", "", context="Исходный запрос: русская кухня",
        rejected=["Том ям"], avoid=["Солянка"],
    ))
    assert [d["name"] for d in res["dishes"]] == ["Борщ", "Треска по-польски", "Плов"]
    user = gate.calls[0][0][-1]["content"]
    assert "Гуляш (говядина)" in user  # заменяемое блюдо с тегами
    assert "Борщ" in user and "Плов" in user  # соседи по плану
    assert "Уже отвергнуто в этой беседе: Том ям" in user
    assert "русская кухня" in user and "Солянка" in user
    assert "Количество блюд" not in user  # без подсказки числа блюд плана


def test_add_direct_appends_one(monkeypatch):
    gate = FakeGate({"reply": "ок", "dish": [_dish("Сырники"), _dish("Омлет")]})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)
    res = asyncio.run(planner.add_dish_direct(PLAN, "План", ""))
    assert len(res["dishes"]) == len(PLAN) + 1
    assert res["dishes"][-1]["name"] == "Сырники"  # без пожелания — первое


def test_pick_one_skips_dishes_already_in_plan():
    got = planner._pick_one([_dish("Борщ"), _dish("Уха")], "", {"борщ"})
    assert got["name"] == "Уха"


def test_variety_hint_excludes_dislikes_and_prefers_rare():
    history = ["Говядина тушёная", "Гуляш из говядины", "Бефстроганов"] * 3
    for seed in range(30):
        text, v = planner._variety_hint(history, ["рыба", "свинина"], random.Random(seed))
        assert "рыба" not in v["proteins"] and "свинина" not in v["proteins"]
        assert len(v["proteins"]) == 2 and 1 <= len(v["methods"]) <= 2
        assert "явный запрос пользователя важнее" in text
    # частая в истории говядина выпадает заметно реже остальных
    picks = [p for s in range(300)
             for p in planner._variety_hint(history, [], random.Random(s))[1]["proteins"]]
    assert picks.count("говядина") < picks.count("бобовые")


def test_plan_prompts_have_no_anchors_and_dynamic_in_user():
    for system in (DEEPSEEK_PLAN_SYSTEM, NAMES_SYSTEM):
        assert "птица, говядина" not in system
        assert "том ям" not in system and "солянка" not in system
        assert "голубцы" not in system
        assert "не больше 2 блюд" in system
    msgs = build_ds_plan_messages(
        "меню", ["Гуляш"], 5, variety="Для разнообразия: …", date_hint="План на неделю 5–11 октября",
    )
    assert msgs[0]["content"] == DEEPSEEK_PLAN_SYSTEM  # system стабилен (кэш префикса)
    user = msgs[1]["content"]
    assert "Недавно ели или отвергли" in user and "Гуляш" in user
    assert "октября" in user and "Для разнообразия" in user


def test_single_dish_prompt_separates_plan_and_history():
    user = build_single_dish_messages(
        "", plan_dishes=[{"name": "Борщ"}], avoid_titles=["Солянка"]
    )[1]["content"]
    assert "Остальные блюда плана" in user and "Недавно ели или отвергли" in user


def test_detail_prompt_has_dish_header_and_request():
    dish = {"tags": ["курица", "панировка"], "prep_min": 20, "cook_min": 25, "garnish": "пюре"}
    user = build_dish_detail_messages("Шницель", 4, dish=dish, request="русская кухня")[1]["content"]
    assert "уложись в эти тайминги" in user and "гарнир: пюре" in user
    assert "русская кухня" in user


def test_like_never_removes_dislike(monkeypatch):
    saved = {}
    monkeypatch.setattr(prefs, "load", lambda: {"dislikes": ["рыба"], "likes": []})
    monkeypatch.setattr(prefs, "_save", lambda d: saved.update(d))
    out = prefs.merge([], ["рыба", "острое"])
    assert out["dislikes"] == ["рыба"] and out["likes"] == ["острое"]


def test_likes_hint_is_soft(monkeypatch):
    monkeypatch.setattr(prefs, "load", lambda: {"dislikes": ["изюм"], "likes": ["азиатская курица"]})
    hint = prefs.as_hint()
    assert "в 1 блюде плана" in hint and "явный запрос" in hint
    assert "азиатская" not in prefs.as_hint(constraints_only=True)


def test_claude_prefill_support():
    assert claude._supports_prefill("claude-haiku-4-5")
    assert not claude._supports_prefill("claude-opus-4-8")
    conv, pre = claude._with_prefill([{"role": "user", "content": "x"}], "claude-haiku-4-5")
    assert pre == "{" and conv[-1] == {"role": "assistant", "content": "{"}


def test_claude_corrective_retry_once(monkeypatch):
    """Проза вместо JSON → одна корректирующая попытка (с ответом модели), не повтор входа."""
    gate = claude.AnthropicGate()
    sent = []
    replies = iter([
        {"content": [{"type": "text", "text": "Вот ваш план: борщ"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"content": [{"type": "text", "text": '"a": 1}'}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 120, "output_tokens": 5}},
    ])

    async def fake_post(payload):
        sent.append(payload)
        return next(replies)

    monkeypatch.setattr(gate, "_post", fake_post)
    parsed, usage = asyncio.run(gate._request_json(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        None, "claude-haiku-4-5", 100, 0.7,
    ))
    assert parsed == {"a": 1} and len(sent) == 2
    fix = sent[1]["messages"]
    assert fix[1] == {"role": "assistant", "content": "{Вот ваш план: борщ"}
    assert "ТОЛЬКО" in fix[2]["content"] and fix[-1]["content"] == "{"
    assert usage["prompt_tokens"] == 220 and usage["completion_tokens"] == 15


def test_claude_truncated_is_not_retried(monkeypatch):
    import pytest

    from app.ai.base import AINonRetryable

    gate = claude.AnthropicGate()

    async def fake_post(payload):
        return {"content": [{"type": "text", "text": '"dishes": [{"na'}], "stop_reason": "max_tokens"}

    monkeypatch.setattr(gate, "_post", fake_post)
    with pytest.raises(AINonRetryable) as err:
        asyncio.run(gate._request_json(
            [{"role": "user", "content": "u"}], None, "claude-opus-4-8", 10, 0
        ))
    assert err.value.details["stop_reason"] == "max_tokens"


def test_dishes_word():
    assert [planner._dishes_word(n) for n in (1, 2, 5, 11, 21)] == [
        "1 блюдо", "2 блюда", "5 блюд", "11 блюд", "21 блюдо",
    ]


def test_edit_dish_variant_goes_under_recipe_model_not_chat_model(monkeypatch):
    """Правка блюда в чате: план правит модель чата (Cloudflare), а рецепт пишет модель задачи
    «Рецепты» (Cloudflare для рецептов запрещён → дефолт DeepSeek). Вариант — под ключом той,
    что реально писала рецепт; раньше он ложился под ключ модели чата («cloudflare»)."""
    from app.ai import gates
    from app.services import settings as app_settings

    monkeypatch.setattr(app_settings, "default_ref", lambda task: "deepseek")
    cf_calls, ds_calls = [], []

    async def cf_complete(messages, **kw):  # правка плана: structured actions
        cf_calls.append(kw.get("label"))
        return {"actions": [{"op": "edit", "name": "Гуляш", "change": "без перца"}]}, {}

    async def ds_complete(messages, **kw):  # рецепт
        ds_calls.append(kw.get("label"))
        return {"ingredients": [{"name": "говядина", "qty": 500, "unit": "г",
                                 "category": "Мясо и птица"}],
                "steps": ["Нарежь", "Туши"], "tips": [], "note": "Разогрей"}, {}

    monkeypatch.setattr(gates.cloudflare, "complete_json", cf_complete)
    monkeypatch.setattr(gates.deepseek, "complete_json", ds_complete)
    dishes = [{"id": "g", "name": "Гуляш", "servings": 4, "uses": ["перец"],
               "storage": {"shelf_life_days": 60},
               "variants": {"gemini": {"ingredients": [], "steps": ["старый"], "tips": [],
                                       "note": "", "provider": "Gemini"}},
               "active_model": "gemini", "steps": ["старый"]}]
    res = asyncio.run(planner.edit_plan(dishes, "План", "Гуляш без перца", model="cloudflare"))
    assert cf_calls == ["правка плана (actions)"] and len(ds_calls) == 1
    d = res["dishes"][0]
    assert set(d["variants"]) == {"gemini", "deepseek"} and d["active_model"] == "deepseek"
    assert d["detail_provider"] == "DeepSeek" and d["steps"] == ["Нарежь", "Туши"]
    v = d["variants"]["deepseek"]
    assert v["kind"] == "chat_edit" and v["change"] == "без перца"
    assert v["model_ref"] == f"deepseek:{gates.deepseek.default_model}"
    assert v["parent_id"] == "gemini" and v["ctx_uses"] == ["перец"] and v["gen_id"]
    assert res["provider"] == gates.cloudflare.provider  # план правила модель чата


def test_detail_carries_model_ref_and_scoped_gen_id(monkeypatch):
    """Деталь рецепта знает, кто её написал (model/model_ref), а gen_id виден в AI-контексте
    только на время самого вызова (строка лога ↔ вариант рецепта)."""
    from app.ai import observe

    seen = []

    class RecipeGate(FakeGate):
        key = "gemini"
        provider = "Gemini"
        default_model = "gemini-flash-lite-latest"

        async def complete_json(self, messages, **kw):
            seen.append(dict(observe._ctx.get()))
            return await super().complete_json(messages, **kw)

    gate = RecipeGate({"ingredients": [{"name": "лук"}], "steps": ["шаг"], "tips": []})
    monkeypatch.setattr(planner, "gate_for", lambda m, task="chat": gate)

    async def run():
        observe.set_ai_context(endpoint="dish_details")
        det = await planner.generate_dish_detail("Суп", 4, model="gemini")
        return det, dict(observe._ctx.get())

    det, after = asyncio.run(run())
    assert det["model"] == "gemini" and det["model_ref"] == "gemini:gemini-flash-lite-latest"
    assert det["provider"] == "Gemini" and len(det["gen_id"]) == 32
    assert seen[0]["gen_id"] == det["gen_id"] and seen[0]["endpoint"] == "dish_details"
    # после вызова gen_id из контекста запроса ушёл (следующий AI-вызов его не унесёт)
    assert after == {"endpoint": "dish_details"}
