"""Рецепты из принятых планов + избранное по нормализованному названию."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings as config
from app.main import app
from app.models import Conversation, FavoriteRecipe, PlanRow


@pytest.fixture()
def session():
    """Временная файловая база тестов (conftest → DB_PATH): та же, что видит приложение из
    потока TestClient. Планы, избранное и таблицы рецептов чистим — список рецептов берёт
    все принятые планы."""
    from sqlmodel import Session, delete

    from app.db import engine, init_db
    from app.models import Recipe, RecipeRevision

    init_db()
    with Session(engine) as s:
        s.exec(delete(FavoriteRecipe))
        s.exec(delete(PlanRow))
        s.exec(delete(RecipeRevision))
        s.exec(delete(Recipe))
        s.commit()
        yield s


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setattr(config, "app_password", "")
    with TestClient(app) as c:
        yield c


def _plan(session, pid, status, dishes, decided=None, title="План"):
    if session.get(Conversation, f"c-{pid}") is None:
        session.add(Conversation(id=f"c-{pid}"))
    session.add(PlanRow(id=pid, conversation_id=f"c-{pid}", title=title, week_label="1–7",
                        status=status, decided_at=decided, dishes=dishes))
    session.commit()


def _dish(i, name, steps=False):
    return {"id": f"d{i}", "name": name, "emoji": "🍲", "prep_min": 10, "cook_min": 30,
            "servings": 4, "tags": ["суп"], "steps": ["шаг"] if steps else []}


def test_only_accepted_newest_first(session, client):
    _plan(session, "old", "accepted", [_dish(1, "Борщ", steps=True)],
          decided=datetime(2026, 9, 1, tzinfo=timezone.utc), title="Старый")
    _plan(session, "new", "accepted", [_dish(1, "Плов"), _dish(2, "Щи")],
          decided=datetime(2026, 9, 20, tzinfo=timezone.utc), title="Новый")
    _plan(session, "draft", "draft", [_dish(1, "Черновое блюдо")])
    _plan(session, "rej", "rejected", [_dish(1, "Отклонённое")])
    items = client.get("/api/recipes").json()
    assert [i["name"] for i in items] == ["Плов", "Щи", "Борщ"]
    assert items[0]["planTitle"] == "Новый" and items[0]["dishId"] == "d1"
    assert items[2]["hasRecipe"] is True and items[0]["hasRecipe"] is False
    assert all(i["favorite"] is False for i in items)


def test_favorite_by_normalized_name_survives_versions(session, client):
    _plan(session, "p1", "accepted", [_dish(1, "Солянка  Сборная"), _dish(2, "Ёжики")])
    r = client.put("/api/recipes/favorite",
                   json={"name": "солянка сборная", "favorite": True, "planId": "p1", "dishId": "d1"})
    assert r.status_code == 200 and r.json() == {"key": "солянка сборная", "favorite": True}
    client.put("/api/recipes/favorite", json={"name": "Ежики", "favorite": True})
    # Новая версия плана (правка) с теми же блюдами — звёзды на месте.
    _plan(session, "p2", "accepted", [_dish(1, "Солянка сборная"), _dish(2, "ёжики")])
    favs = {i["planId"] + ":" + i["name"]: i["favorite"] for i in client.get("/api/recipes").json()}
    assert favs["p2:Солянка сборная"] and favs["p2:ёжики"] and favs["p1:Солянка  Сборная"]
    # Повторная отметка не плодит записей; снятие удаляет.
    client.put("/api/recipes/favorite", json={"name": "Солянка сборная", "favorite": True})
    from sqlmodel import select

    session.expire_all()
    assert len(session.exec(select(FavoriteRecipe)).all()) == 2
    client.put("/api/recipes/favorite", json={"name": "СОЛЯНКА СБОРНАЯ", "favorite": False})
    items = client.get("/api/recipes").json()
    assert not any(i["favorite"] for i in items if "солянка" in i["key"])


def test_favorite_validation(client):
    assert client.put("/api/recipes/favorite", json={"name": "", "favorite": True}).status_code == 422
    assert client.put("/api/recipes/favorite", json={"favorite": True}).status_code == 422
