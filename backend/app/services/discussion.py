"""Обсуждение цели в чате («💬 Обсудить в чате»): рецепт блюда, план готовки, список покупок.

Реплики обсуждения — обычные MessageRow беседы плана с пометкой discuss_target (+ dish_id для
рецепта). Отсюда же берём контекст «↻ Перегенерировать»: что пользователь просил в обсуждении
этой цели. Всё детерминированно из БД; держим компактным (≤12 реплик / ≤2500 символов),
чтобы не раздувать токены.
"""

from sqlmodel import Session, select

from ..models import MessageRow
from .history import norm_name

TARGETS = ("recipe", "cooking", "shopping")

_MAX_MSGS = 12
_MAX_CHARS = 2500
_MSG_CLIP = 600  # одна реплика в контексте — не длиннее


def _clip(text: str, n: int = _MSG_CLIP) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _cap(msgs: list[MessageRow]) -> list[MessageRow]:
    """Хвост треда в пределах лимитов: берём с конца, пока влезает по числу и символам."""
    out: list[MessageRow] = []
    total = 0
    for m in reversed(msgs):
        size = len(_clip(m.text))
        if len(out) >= _MAX_MSGS or (out and total + size > _MAX_CHARS):
            break
        out.append(m)
        total += size
    return list(reversed(out))


def thread(
    session: Session, conversation_id: str | None, target: str, dish_id: str | None = None
) -> list[MessageRow]:
    """Все реплики обсуждения цели в беседе (по времени). Для recipe — только этого блюда."""
    if not conversation_id:
        return []
    q = select(MessageRow).where(
        MessageRow.conversation_id == conversation_id,
        MessageRow.discuss_target == target,
    )
    if target == "recipe":
        q = q.where(MessageRow.dish_id == dish_id)
    rows = session.exec(q.order_by(MessageRow.created_at)).all()
    return [m for m in rows if (m.text or "").strip()]


def _mentions(session: Session, conversation_id: str, dish_name: str) -> list[MessageRow]:
    """Фолбэк для рецепта: обычные реплики беседы, где упомянуто блюдо (напр. правка
    «убери перец из рагу» или ответ «рецепт «Рагу» обновлён»). Лейблы кнопок замены — мимо."""
    name = norm_name(dish_name)
    if not name:
        return []
    first = name.split(" ")[0]
    rows = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id, MessageRow.discuss_target.is_(None))
        .order_by(MessageRow.created_at)
    ).all()
    out: list[MessageRow] = []
    for m in rows:
        text = (m.text or "").strip()
        if not text or text.startswith("Замена «"):
            continue
        t = norm_name(text)
        if name in t or (len(first) >= 5 and first in t):
            out.append(m)
    return out


def discussion_text(
    session: Session,
    conversation_id: str | None,
    target: str,
    dish_id: str | None = None,
    dish_name: str = "",
    skip_first_user: str = "",
) -> str:
    """Обсуждение цели одной строкой-диалогом для USER-части промпта перегенерации.
    Пусто — пожеланий нет. skip_first_user — исходный запрос (он идёт в промпт отдельно)."""
    msgs = thread(session, conversation_id, target, dish_id)
    if not msgs and target == "recipe" and conversation_id:
        msgs = _mentions(session, conversation_id, dish_name)
    skip = (skip_first_user or "").strip()
    msgs = [m for m in msgs if not (m.role == "user" and (m.text or "").strip() == skip)]
    lines = [
        f"{'Пользователь' if m.role == 'user' else 'Ты'}: {_clip(m.text)}" for m in _cap(msgs)
    ]
    return "\n".join(lines)


def discuss_turns(
    session: Session, conversation_id: str | None, target: str, dish_id: str | None = None
) -> list[dict[str, str]]:
    """Прошлые реплики обсуждения цели как мульти-тёрн (role/content), в пределах лимитов.
    Подряд идущие реплики одной роли склеиваем (Claude/Gemini требуют чередования)."""
    out: list[dict[str, str]] = []
    for m in _cap(thread(session, conversation_id, target, dish_id)):
        role = "assistant" if m.role == "assistant" else "user"
        text = _clip(m.text)
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    return out
