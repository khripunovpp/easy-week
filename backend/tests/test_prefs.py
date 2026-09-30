"""Предпочтения: загрузка старого файла, атомарная запись, merge (аллергии не трогает),
частичный PUT с валидацией, хинт в промпты."""

import json

import pytest
from fastapi.testclient import TestClient

from app.ai import prefs
from app.ai.prompt import build_cook_plan_messages, build_discuss_messages


@pytest.fixture()
def pfile(tmp_path, monkeypatch):
    """Настоящий файл предпочтений во временном каталоге (conftest глушит load — возвращаем)."""
    f = tmp_path / "preferences.json"
    monkeypatch.setattr(prefs, "_file", lambda: f)
    monkeypatch.setattr(prefs, "load", prefs._load_file)
    return f


def test_load_old_format_migrates(pfile):
    # старый файл: только likes/dislikes, аллергии были вперемешку с нелюбимым
    pfile.write_text(json.dumps({"dislikes": ["рыба", "Рыба", " "], "likes": ["острое"]}), "utf-8")
    data = prefs.load()
    assert data["dislikes"] == ["рыба"] and data["likes"] == ["острое"]
    assert data["allergies"] == [] and data["suggested_allergies"] == []
    assert data["macros"] == {"protein": "normal", "fat": "normal", "carbs": "normal"}
    assert data["diet_note"] == ""


def test_load_broken_file_is_empty(pfile):
    pfile.write_text("{не json", "utf-8")
    assert prefs.load()["allergies"] == []


def test_save_is_atomic_and_leaves_no_tmp(pfile):
    prefs.update({"allergies": ["арахис"]})
    assert json.loads(pfile.read_text("utf-8"))["allergies"] == ["арахис"]
    assert not list(pfile.parent.glob("*.tmp"))


def test_merge_never_touches_allergies(pfile):
    prefs.update({"allergies": ["арахис"], "likes": ["креветки"]})
    out = prefs.merge(["кинза"], ["арахис", "манго"], ["креветки", "Арахис"])
    assert out["allergies"] == ["арахис"]  # экстрактор аллергии не меняет
    assert out["suggested_allergies"] == ["креветки"]  # уже-аллергия не предлагается
    assert "креветки" not in out["likes"]  # подозрение на аллерген снимает «люблю»
    assert "арахис" not in out["likes"] and "манго" in out["likes"]  # лайк аллергену не пишем
    assert out["dislikes"] == ["кинза"]
    # принятая подсказка исчезает из suggested
    out = prefs.update({"allergies": ["арахис", "креветки"]})
    assert out["suggested_allergies"] == []


def test_merge_trims_long_items_and_caps(pfile):
    out = prefs.merge(["х" * 80] + [f"d{i}" for i in range(40)], [])
    assert len(out["dislikes"]) == prefs.MAX_ITEMS
    assert len(out["dislikes"][0]) == prefs.MAX_ITEM_LEN


def test_put_partial_keeps_other_fields(pfile):
    from app.main import app

    client = TestClient(app)
    prefs.update({"allergies": ["арахис"], "macros": {"protein": "high"}})
    # старый клиент (профиль) шлёт только likes/dislikes — аллергии и БЖУ не стираются
    r = client.put("/api/preferences", json={"dislikes": ["рыба"], "likes": []})
    assert r.status_code == 200
    body = r.json()
    assert body["allergies"] == ["арахис"] and body["dislikes"] == ["рыба"]
    assert body["macros"]["protein"] == "high" and "suggestedAllergies" in body
    r = client.put("/api/preferences", json={"macros": {"fat": "low"}, "dietNote": " без жарки "})
    body = r.json()
    assert body["macros"] == {"protein": "high", "fat": "low", "carbs": "normal"}
    assert body["dietNote"] == "без жарки"
    assert client.get("/api/preferences").json()["allergies"] == ["арахис"]


@pytest.mark.parametrize(
    "payload",
    [
        {"allergies": ["х" * 41]},
        {"likes": [f"x{i}" for i in range(31)]},
        {"macros": {"protein": "max"}},
        {"dietNote": "х" * 201},
        {"dislikes": [""]},
    ],
)
def test_put_rejects_bad_input(pfile, payload):
    from app.main import app

    assert TestClient(app).put("/api/preferences", json=payload).status_code == 422


def _fake(monkeypatch, **data):
    monkeypatch.setattr(prefs, "load", lambda: prefs.normalize(data))


def test_hint_order_and_hardness(monkeypatch):
    _fake(
        monkeypatch, allergies=["арахис"], dislikes=["изюм"], suggested_allergies=["киви"],
        likes=["острое"], macros={"protein": "high", "fat": "low"},
    )
    hint = prefs.as_hint()
    assert hint.index("АЛЛЕРГИИ") < hint.index("НЕ используй") < hint.index("Любимое")
    assert "включая следы/соусы: арахис" in hint
    assert "изюм, киви" in hint  # подозрения — тоже ограничение, пока не решено
    assert "БЖУ: больше белка, меньше жиров" in hint
    detail = prefs.as_hint(constraints_only=True)
    assert "АЛЛЕРГИИ" in detail and "острое" not in detail and "БЖУ" in detail
    assert "БЖУ" not in prefs.as_hint(constraints_only=True, macros=False)


def test_hint_macros_all_normal_silent(monkeypatch):
    _fake(monkeypatch, likes=["острое"])
    assert "БЖУ" not in prefs.as_hint()
    _fake(monkeypatch)
    assert prefs.as_hint() == ""


def test_allergies_in_cooking_and_discuss_but_not_shopping(monkeypatch):
    _fake(monkeypatch, allergies=["арахис"], macros={"carbs": "low"})
    cook = build_cook_plan_messages([{"name": "Плов"}])[1]["content"]
    assert "АЛЛЕРГИИ" in cook and "БЖУ" not in cook
    recipe = build_discuss_messages("recipe", "ctx", [], "можно острее?")
    assert "АЛЛЕРГИИ" in recipe[1]["content"] and "меньше углеводов" in recipe[1]["content"]
    assert "АЛЛЕРГИИ" not in recipe[0]["content"]  # system стабилен
    shop = build_discuss_messages("shopping", "ctx", [], "что докупить?")
    assert "АЛЛЕРГИИ" not in shop[1]["content"]


def test_variety_bans_allergens(monkeypatch):
    from app.ai import planner

    _fake(monkeypatch, allergies=["креветки"])
    assert "креветки" in prefs.avoid_all()
    assert planner._variety_hint([])  # не падает с аллергиями


class _FakeGate:
    provider = "Fake"
    key = "fake"

    def __init__(self, parsed):
        self.parsed = parsed
        self.kw = None

    async def complete_json(self, messages, **kw):
        self.kw = kw
        return self.parsed, {}


def test_extract_uses_prefs_task_model(pfile, monkeypatch):
    """Экстрактор берёт модель задачи `prefs` из настроек; не-Cloudflare — без json_schema."""
    import asyncio

    gate = _FakeGate({"dislikes": ["кинза"], "likes": [], "allergies": []})
    seen = []
    monkeypatch.setattr(prefs, "gate_for", lambda m, task="chat": seen.append((m, task)) or gate)
    asyncio.run(prefs.extract_and_merge("не люблю кинзу"))
    assert seen == [("", "prefs")]
    assert "schema" not in gate.kw and "model" not in gate.kw
    assert prefs.load()["dislikes"] == ["кинза"]


def test_extract_cloudflare_passes_schema(pfile, monkeypatch):
    from app.config import settings as config

    gate = _FakeGate({"dislikes": [], "likes": [], "allergies": []})
    monkeypatch.setattr(prefs, "cloudflare", gate)
    monkeypatch.setattr(prefs, "gate_for", lambda m, task="chat": gate)
    import asyncio

    asyncio.run(prefs.extract_and_merge("терпеть не могу печень"))
    assert gate.kw["schema"] is prefs.PREFS_SCHEMA and gate.kw["model"] == config.cf_model_judge



# --- три уровня уверенности и страховки без модели ---

def _run(monkeypatch, message, parsed):
    gate = _FakeGate({"dislikes": [], "likes": [], "maybe_dislikes": [], "maybe_likes": [],
                      "allergies": [], **parsed})
    monkeypatch.setattr(prefs, "gate_for", lambda m, task="chat": gate)
    import asyncio

    asyncio.run(prefs.extract_and_merge(message))
    return gate


def test_no_taste_words_no_model_call(pfile, monkeypatch):
    for msg in ("в эту неделю давай без рыбы", "убери грибы", "сделай 5 ужинов побыстрее"):
        gate = _run(monkeypatch, msg, {"dislikes": ["рыба"]})
        assert gate.kw is None, msg  # модель не звали — ложному «не люблю» неоткуда взяться
    assert prefs.load()["dislikes"] == []


def test_temporary_wish_becomes_only_suggestion(pfile, monkeypatch):
    _run(monkeypatch, "на этой неделе хочу то, что люблю — курицу", {"likes": ["курица"]})
    data = prefs.load()
    assert data["likes"] == [] and data["suggested_likes"] == ["курица"]


def test_permanent_words_beat_week_marker(pfile, monkeypatch):
    _run(monkeypatch, "на этой неделе без свинины, мы её вообще не едим", {"dislikes": ["свинина"]})
    assert prefs.load()["dislikes"] == ["свинина"]


def test_maybe_goes_to_suggestions_and_hallucinations_dropped(pfile, monkeypatch):
    _run(monkeypatch, "баклажаны что-то не очень люблю",
         {"maybe_dislikes": ["баклажаны"], "dislikes": ["бобы"]})  # «бобы» в тексте нет
    data = prefs.load()
    assert data["dislikes"] == [] and data["suggested_dislikes"] == ["баклажаны"]


def test_confirming_suggestion_clears_it(pfile):
    prefs.merge([], [], [], ["баклажаны"], ["солянка"])
    out = prefs.update({"dislikes": ["Баклажаны"], "likes": ["солянка"]})
    assert out["suggested_dislikes"] == [] and out["suggested_likes"] == []
    assert out["dislikes"] == ["Баклажаны"]


def test_suggestions_not_in_prompt_hint(pfile):
    prefs.merge([], [], [], ["баклажаны"], ["солянка"])
    hint = prefs.as_hint()
    assert "баклажаны" not in hint and "солянка" not in hint  # неподтверждённое — не ограничение


def test_put_accepts_suggestion_fields(pfile):
    from fastapi.testclient import TestClient

    from app.config import settings as config
    from app.main import app

    config_pw = config.app_password
    config.app_password = ""
    try:
        prefs.merge([], [], [], ["баклажаны"], [])
        r = TestClient(app).put("/api/preferences", json={"suggestedDislikes": []})
        assert r.status_code == 200 and r.json()["suggestedDislikes"] == []
    finally:
        config.app_password = config_pw


def test_review_of_dish_is_only_suggestion(pfile, monkeypatch):
    _run(monkeypatch, "прошлая солянка очень понравилась", {"likes": ["солянка"]})
    data = prefs.load()
    assert data["likes"] == [] and data["suggested_likes"] == ["солянка"]
