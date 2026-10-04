"""services/history.py: список «недавно ели или отвергли» и память беседы."""

import random
from datetime import datetime, timedelta, timezone

from app.models import Conversation, MessageRow, PlanRow, RatingRow
from app.services import history


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dishes(*names):
    return [{"id": f"d{i}", "name": n} for i, n in enumerate(names)]


def _plan(session, pid, conv, names, status="draft", parent=None, created=None, decided=None):
    if session.get(Conversation, conv) is None:
        session.add(Conversation(id=conv))
    row = PlanRow(
        id=pid, conversation_id=conv, title="t", week_label="w", status=status,
        parent_id=parent, dishes=_dishes(*names),
        created_at=created or _now(), decided_at=decided,
    )
    session.add(row)
    session.commit()
    return row


def test_recent_accepted_sorted_by_decision_and_limited(session):
    base = _now() - timedelta(days=20)
    for i in range(6):
        # decided_at растёт с i — самые свежие решения у больших i
        _plan(session, f"p{i}", f"c{i}", [f"Блюдо {i}"], status="accepted",
              created=base, decided=base + timedelta(days=i))
    got = history.recent_accepted(session)
    assert got == ["Блюдо 5", "Блюдо 4", "Блюдо 3", "Блюдо 2"]


def test_replaced_or_removed_by_version_diff(session):
    _plan(session, "v1", "c", ["Борщ", "Плов", "Котлеты"], status="rejected")
    _plan(session, "v2", "c", ["Борщ", "Лагман", "Котлеты"], parent="v1")  # Плов заменён
    _plan(session, "v3", "c", ["Борщ", "Лагман"], parent="v2")  # Котлеты удалены
    # старая правка (> 30 дней) — не учитываем
    _plan(session, "o1", "old", ["Солянка"], status="rejected", created=_now() - timedelta(days=60))
    _plan(session, "o2", "old", ["Щи"], parent="o1", created=_now() - timedelta(days=59))
    got = history.replaced_or_removed(session)
    assert set(got) == {"Плов", "Котлеты"}


def test_conversation_rejected_includes_replace_messages(session):
    _plan(session, "v1", "c", ["Том ям", "Плов"], status="rejected")
    _plan(session, "v2", "c", ["Рамен", "Плов"], parent="v1")
    session.add(MessageRow(id="m1", conversation_id="c", role="user", text="Замена «Рамен»: не суп"))
    session.add(MessageRow(id="m2", conversation_id="c", role="user", text="хочу рыбу"))
    session.commit()
    assert history.conversation_rejected(session, "c") == ["Том ям", "Рамен"]


def test_variety_avoid_dedupes_and_includes_dislikes_and_drafts(session):
    _plan(session, "a1", "c1", ["Гуляш", "Борщ"], status="accepted", decided=_now())
    _plan(session, "a2", "c2", ["гуляш"], status="accepted", decided=_now())  # дубль по регистру
    _plan(session, "d1", "c3", ["Паэлья"])  # брошенный черновик
    _plan(session, "cur", "current", ["Текущее"])  # черновик текущей беседы — не берём
    session.add(RatingRow(id="r1", target_type="recipe", target_id="d0", vote=-1,
                          plan_id="d1", dish_id="d0"))
    session.commit()
    got = history.variety_avoid(session, exclude_conversation="current", rng=random.Random(1))
    assert [history.norm_name(n) for n in got].count("гуляш") == 1
    assert "Паэлья" in got and "Борщ" in got
    assert "Текущее" not in got


def test_variety_avoid_caps_with_random_sample(session):
    _plan(session, "a1", "c1", [f"Блюдо {i}" for i in range(50)], status="accepted",
          decided=_now())
    got = history.variety_avoid(session, cap=30, rng=random.Random(7))
    assert len(got) == 30
    # выборка сохраняет исходный порядок
    idx = [int(n.split()[-1]) for n in got]
    assert idx == sorted(idx)


def test_original_request_and_reply_mention(session):
    _plan(session, "p", "c", ["Шницель куриный"])
    session.add(MessageRow(id="u1", conversation_id="c", role="user", text="  "))
    session.add(MessageRow(id="u2", conversation_id="c", role="user", text="Русская кухня"))
    session.add(MessageRow(id="a1", conversation_id="c", role="assistant", plan_id="p",
                           text="Шницель сделаем в панировке с горчицей."))
    session.commit()
    assert history.original_request(session, "c") == "Русская кухня"
    assert "горчиц" in history.reply_mention(session, "p", "Шницель куриный")
    assert history.reply_mention(session, "p", "Плов") == ""


def test_original_request_is_empty_for_library(session):
    """Беседа «Моих рецептов»: там только реплики обсуждения своих рецептов — первая из них
    не «исходный запрос плана» и не должна уходить в каждый рецепт."""
    from app.services.recipebook import LIBRARY_ID, library_row

    library_row(session)
    session.add(MessageRow(id="l1", conversation_id=LIBRARY_ID, role="user",
                           text="Можно сырники без сахара?", discuss_target="recipe",
                           dish_id="own-0-syrniki"))
    session.commit()
    assert history.original_request(session, LIBRARY_ID) == ""
