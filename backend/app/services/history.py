"""История блюд для разнообразия: что недавно ели или отвергли — чтобы модель не повторялась.

Источники (всё детерминированно из БД, без модели):
- принятые планы — последние N по времени решения (decided_at, иначе created_at);
- заменённые/удалённые блюда за последние дни — разница «версия-родитель → версия-потомок»
  (parent_id). Статус 'rejected' сам по себе шумный: каждая правка в чате автоматически
  отклоняет родителя, поэтому смотрим именно на то, какие блюда пропали между версиями;
- брошенные черновики — последние версии бесед без принятого плана (немного, случайно);
- 👎 рецептам (RatingRow target_type=recipe) — если по plan_id/dish_id находится название.
"""

import random
import re
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from ..models import MessageRow, PlanRow, RatingRow

_RECENT_ACCEPTED = 4  # сколько последних принятых планов берём
_REJECT_DAYS = 30  # окно для заменённых/удалённых и черновиков
_DRAFT_SAMPLE = 5  # сколько блюд случайно берём из брошенных черновиков

# «Замена «X»» / «Замена «X»: пожелание» — лейбл кнопки замены в ленте чата (routers/chat.py).
_REPLACE_MSG_RE = re.compile(r"^Замена «(.+?)»")


def norm_name(name: str) -> str:
    """Ключ сравнения названий: нижний регистр, ё→е, схлопнутые пробелы."""
    return re.sub(r"\s+", " ", (name or "").lower().replace("ё", "е")).strip()


def _naive(dt: datetime | None) -> datetime:
    # SQLite отдаёт naive-datetime, а в памяти бывают aware — сравниваем в naive UTC.
    if dt is None:
        return datetime.min
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _names(dishes: list | None) -> list[str]:
    return [str(d.get("name")).strip() for d in (dishes or []) if d.get("name")]


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = norm_name(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _removed(parent: PlanRow, child: PlanRow) -> list[str]:
    """Блюда, которые были в родительской версии и пропали в потомке (замена/удаление)."""
    kept = {norm_name(n) for n in _names(child.dishes)}
    return [n for n in _names(parent.dishes) if norm_name(n) not in kept]


def recent_accepted(session: Session, plans: int = _RECENT_ACCEPTED) -> list[str]:
    """Блюда последних принятых планов — свежие первыми."""
    rows = session.exec(select(PlanRow).where(PlanRow.status == "accepted")).all()
    rows = sorted(rows, key=lambda r: _naive(r.decided_at or r.created_at), reverse=True)
    return [n for r in rows[:plans] for n in _names(r.dishes)]


def replaced_or_removed(session: Session, days: int = _REJECT_DAYS) -> list[str]:
    """Заменённые/удалённые за последние days дней — по разнице версий плана."""
    cutoff = _now() - timedelta(days=days)
    children = [
        r for r in session.exec(select(PlanRow).where(PlanRow.parent_id.is_not(None))).all()
        if _naive(r.created_at) >= cutoff
    ]
    out: list[str] = []
    for child in sorted(children, key=lambda r: _naive(r.created_at), reverse=True):
        parent = session.get(PlanRow, child.parent_id)
        if parent is not None:
            out.extend(_removed(parent, child))
    return out


def abandoned_drafts(
    session: Session, days: int = _REJECT_DAYS, exclude_conversation: str | None = None
) -> list[str]:
    """Блюда брошенных черновиков: последняя версия беседы, где ничего не приняли."""
    cutoff = _now() - timedelta(days=days)
    rows = session.exec(select(PlanRow)).all()
    superseded = {r.parent_id for r in rows if r.parent_id}
    accepted_convs = {r.conversation_id for r in rows if r.status == "accepted"}
    out: list[str] = []
    for r in rows:
        if (
            r.status == "draft"
            and r.id not in superseded
            and r.conversation_id not in accepted_convs
            and r.conversation_id != exclude_conversation
            and _naive(r.created_at) >= cutoff
        ):
            out.extend(_names(r.dishes))
    return out


def disliked_recipes(session: Session) -> list[str]:
    """Названия блюд с 👎 за рецепт (если план/блюдо ещё существуют)."""
    rows = session.exec(
        select(RatingRow).where(RatingRow.target_type == "recipe", RatingRow.vote == -1)
    ).all()
    out: list[str] = []
    for r in rows:
        plan = session.get(PlanRow, r.plan_id) if r.plan_id else None
        if plan is None:
            continue
        dish_id = r.dish_id or r.target_id
        name = next((d.get("name") for d in (plan.dishes or []) if d.get("id") == dish_id), None)
        if name:
            out.append(str(name))
    return out


def variety_avoid(
    session: Session,
    cap: int = 30,
    exclude_conversation: str | None = None,
    rng: random.Random | None = None,
) -> list[str]:
    """Список «недавно ели или отвергли» для промпта генерации (не больше cap названий).

    Порядок приоритета: принятые → заменённые/удалённые → 👎 → сэмпл черновиков. Если после
    дедупа названий больше cap — берём случайную выборку (порядок сохраняем), чтобы список
    в разных беседах отличался и не «застывал»."""
    rng = rng or random.Random()
    drafts = _dedupe(abandoned_drafts(session, exclude_conversation=exclude_conversation))
    names = _dedupe(
        recent_accepted(session)
        + replaced_or_removed(session)
        + disliked_recipes(session)
        + rng.sample(drafts, min(_DRAFT_SAMPLE, len(drafts)))
    )
    if len(names) <= cap:
        return names
    keep = set(rng.sample(range(len(names)), cap))
    return [n for i, n in enumerate(names) if i in keep]


def conversation_rejected(session: Session, conversation_id: str) -> list[str]:
    """Что уже отвергнуто В ЭТОЙ беседе: блюда, пропавшие между версиями плана,
    + названия из реплик «Замена «X»» (кнопка замены)."""
    plans = session.exec(
        select(PlanRow)
        .where(PlanRow.conversation_id == conversation_id)
        .order_by(PlanRow.created_at)
    ).all()
    by_id = {p.id: p for p in plans}
    out: list[str] = []
    for p in plans:
        parent = by_id.get(p.parent_id) if p.parent_id else None
        if parent is not None:
            out.extend(_removed(parent, p))
    msgs = session.exec(
        select(MessageRow).where(
            MessageRow.conversation_id == conversation_id, MessageRow.role == "user"
        )
    ).all()
    for m in msgs:
        mt = _REPLACE_MSG_RE.match(m.text or "")
        if mt:
            out.append(mt.group(1))
    return _dedupe(out)


def original_request(session: Session, conversation_id: str | None) -> str:
    """Первое содержательное сообщение пользователя в беседе (исходный запрос плана)."""
    if not conversation_id:
        return ""
    msgs = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id, MessageRow.role == "user")
        .order_by(MessageRow.created_at)
    ).all()
    first = next((m for m in msgs if (m.text or "").strip()), None)
    return first.text.strip() if first else ""


def reply_mention(session: Session, plan_id: str, dish_name: str) -> str:
    """Реплика ассистента к плану — только если в ней упомянуто это блюдо (там бывают
    обещания вроде «в панировке с горчицей», которые деталь должна выполнить)."""
    msg = session.exec(
        select(MessageRow).where(MessageRow.plan_id == plan_id, MessageRow.role == "assistant")
    ).first()
    text = (msg.text or "").strip() if msg else ""
    name = norm_name(dish_name)
    # «Готово: «X» заменено на «Y»» — служебная реплика правки, пользы детали не несёт.
    if not text or not name or text.startswith("Готово:"):
        return ""
    t = norm_name(text)
    first = name.split(" ")[0]
    return text if name in t or (len(first) >= 5 and first in t) else ""
