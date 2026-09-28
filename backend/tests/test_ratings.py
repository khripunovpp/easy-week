"""Оценки 👍/👎 и причины 👎: PUT /api/ratings/reasons, каталог, сброс причин при смене голоса."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import settings as config
from app.main import app
from app.services import rating_reasons


@pytest.fixture(autouse=True)
def _no_password(monkeypatch):
    monkeypatch.setattr(config, "app_password", "")


def _target() -> dict:
    return {"targetType": "recipe", "targetId": f"dish-{uuid4().hex}", "model": "anthropic"}


def test_catalog_has_other_last():
    with TestClient(app) as c:
        cat = c.get("/api/ratings/reasons").json()
    assert set(cat) == set(rating_reasons.REASONS)
    for items in cat.values():
        assert items[-1] == {"key": "other", "label": "Другое"}


def test_reasons_need_dislike():
    t = _target()
    with TestClient(app) as c:
        assert c.put("/api/ratings/reasons", json={**t, "reasons": ["wrong"]}).status_code == 409
        c.post("/api/ratings", json={**t, "vote": 1})
        assert c.put("/api/ratings/reasons", json={**t, "reasons": ["wrong"]}).status_code == 409


def test_reasons_saved_and_cleaned(session):
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RatingRow

    t = _target()
    with TestClient(app) as c:
        c.post("/api/ratings", json={**t, "vote": -1})
        r = c.put(
            "/api/ratings/reasons",
            # мусорный ключ и ключ чужого типа отбрасываются; текст → добавляет «other»
            json={**t, "reasons": ["too_long", "bogus", "duplicates", "wrong"], "note": " сухо "},
        )
        assert r.status_code == 200 and r.json() == {"vote": -1}
        with Session(engine) as s:
            row = s.exec(select(RatingRow).where(RatingRow.target_id == t["targetId"])).one()
            assert row.reasons == "wrong,too_long,other"
            assert row.note == "сухо"
        # Смена голоса сбрасывает причины.
        c.post("/api/ratings", json={**t, "vote": 1})
        with Session(engine) as s:
            row = s.exec(select(RatingRow).where(RatingRow.target_id == t["targetId"])).one()
            assert row.reasons == "" and row.vote == 1
