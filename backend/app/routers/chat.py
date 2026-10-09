import copy
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlmodel import Session, select

from ..ai import prefs
from ..ai.base import AIError
from ..ai.gates import resolve_key
from ..ai.limits import LimitError, status as limits_status
from ..ai.observe import record_conversation, record_plan, set_ai_context
from ..ai.prompt import chat_memory_block, leftovers_status
from ..ai.planner import (
    _week_label,
    add_dish_direct,
    edit_plan,
    generate_plan,
    generate_plan_stream,
    remove_dish_by_id,
    replace_dish_by_id,
)
from ..db import get_session
from ..models import Conversation, MessageRow, PlanRow
from ..schemas import (
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    CurrentPlanBody,
    Dish,
    MessageSearchHit,
    PreferencesBody,
    PreferencesOut,
)
from ..services import appstate, planstore
from ..services import summary as chat_summary
from ..services.history import conversation_rejected, is_repeat, variety_avoid
from ..services.recipebook import (
    LIBRARY_ID,
    LIBRARY_STATUS,
    attach,
    attach_all,
    book_index,
    book_names,
)
from ..services.shopping import sync_uses
from ..services.mapping import to_week_plan

import logging

router = APIRouter(prefix="/api", tags=["chat"])

logger = logging.getLogger("easy_week.chat")

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/limits")
async def get_limits() -> dict:
    """Дневные лимиты за сегодня (used/limit/remaining): генерации Claude и озвучка шагов."""
    from ..ai.limits import tts_status

    return {"anthropic": limits_status(), "tts": tts_status()}


@router.get("/preferences")
async def get_preferences() -> PreferencesOut:
    """Пищевые предпочтения: аллергии, любит / не любит, БЖУ, подсказки аллергий из чата."""
    return PreferencesOut(**prefs.load())


@router.put("/preferences")
async def put_preferences(body: PreferencesBody) -> PreferencesOut:
    """Частичная правка (экран /preferences): меняем только переданные поля, остальное как было.
    Невалидное (пункт > 40 симв., > 30 пунктов, БЖУ не low|normal|high) — 422 от pydantic."""
    patch = body.model_dump(exclude_none=True)
    return PreferencesOut(**prefs.update(patch))


@router.get("/current-plan")
async def get_current_plan(session: SessionDep) -> dict:
    """Выбранный «текущий» план (общий для покупок/готовки). null, если не выбран/удалён."""
    pid = appstate.get_current_plan()
    if pid and session.get(PlanRow, pid) is None:
        pid = None
    # Правка в чате (убрать/заменить/добавить блюдо) создаёт новую версию плана — текущим
    # должен быть её последний потомок, иначе покупки/готовка показывают старый состав.
    latest = _latest_version(session, pid) if pid else None
    if latest and latest != pid:
        appstate.set_current_plan(latest)
        pid = latest
    return {"planId": pid}


def _latest_version(session: Session, plan_id: str) -> str:
    """Последняя версия плана по цепочке правок (planstore.latest_version)."""
    return planstore.latest_version(session, plan_id)


@router.put("/current-plan")
async def set_current_plan(body: CurrentPlanBody, session: SessionDep) -> dict:
    """Выбрать «текущий» план (для покупок/готовки), общий для всех устройств."""
    pid = body.plan_id
    if pid and session.get(PlanRow, pid) is None:
        raise HTTPException(status_code=404, detail="План не найден")
    appstate.set_current_plan(pid)
    return {"planId": pid}


@router.get("/messages/search")
async def search_messages(q: str, session: SessionDep) -> list[MessageSearchHit]:
    """Поиск по тексту сообщений всех бесед (регистронезависимо, в т.ч. кириллица).
    Возвращает само сообщение + контекст беседы (последний план: название + эмодзи)."""
    query = (q or "").strip().lower()
    if not query:
        return []
    # Последний план каждой беседы — для контекста результата.
    plans = session.exec(select(PlanRow).order_by(PlanRow.created_at.desc())).all()
    conv_plan: dict[str, PlanRow] = {}
    for p in plans:
        conv_plan.setdefault(p.conversation_id, p)  # первый встреченный = самый свежий
    rows = session.exec(select(MessageRow).order_by(MessageRow.created_at.desc())).all()
    hits: list[MessageSearchHit] = []
    for m in rows:
        if not m.text or query not in m.text.lower():
            continue
        plan_row = session.get(PlanRow, m.plan_id) if m.plan_id else conv_plan.get(m.conversation_id)
        title = plan_row.title if plan_row else None
        emoji = None
        if plan_row:
            dishes = plan_row.dishes or []
            emoji = dishes[0].get("emoji", "🍽️") if dishes else "🍽️"
        hits.append(
            MessageSearchHit(
                id=m.id,
                conversation_id=m.conversation_id,
                role=m.role,
                text=m.text,
                plan_title=title,
                plan_emoji=emoji,
            )
        )
        if len(hits) >= 50:
            break
    return hits


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: str, session: SessionDep
) -> list[ChatMessageOut]:
    """Сообщения диалога (для продолжения обсуждения плана в чате)."""
    rows = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at)
    ).all()
    out: list[ChatMessageOut] = []
    # Реплики обсуждения ссылаются на последнюю версию плана беседы (id блюд между версиями
    # сохраняются) — для ссылки «Открыть рецепт/план готовки/покупки». В беседе «Моих
    # рецептов» обсуждают свои рецепты — ссылка ведёт в служебный план library.
    latest = (
        _latest_plan(session, conversation_id, with_library=True)
        if any(m.discuss_target for m in rows) else None
    )
    for m in rows:
        plan = None
        if m.plan_id:
            plan_row = session.get(PlanRow, m.plan_id)
            if plan_row:
                plan = to_week_plan(plan_row)
        out.append(ChatMessageOut(
            id=m.id, role=m.role, text=m.text, plan=plan, model=m.model or "",
            discuss_target=m.discuss_target, dish_id=m.dish_id,
            discuss_plan_id=latest.id if (m.discuss_target and latest) else None,
        ))
    return out


@router.post("/chat/stream", response_class=EventSourceResponse)
async def chat_stream(
    req: ChatRequest, session: SessionDep
) -> AsyncIterable[ServerSentEvent]:
    """Потоковый чат: события meta → dish (по одному) → done. То же, что /chat,
    но блюда прилетают по мере генерации (SSE). Токенов не больше — один вызов модели."""
    # Пусто → модель «Чат и план» по умолчанию; дальше везде (и в сообщениях) — реальный ключ.
    req.recipe_model = resolve_key(req.recipe_model, "chat")
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        conv = Conversation(id=uuid4().hex)
        session.add(conv)
        session.commit()
        record_conversation()
    # Беседа — в контекст AI-лога ДО фоновых задач (задача копирует контекст при создании).
    set_ai_context(conversation_id=conv.id, endpoint="chat_stream")
    if _save_user_message(session, conv.id, req.message, resend=req.resend):
        prefs.learn_async(req.message)  # фоново запоминаем предпочтения (CF, бесплатно)
        chat_summary.schedule(conv.id)  # сводка беседы — фоном, дебаунс 5 с
    # «Недавно ели или отвергли» — свежие принятые, заменённые/удалённые, 👎, черновики.
    avoid = variety_avoid(session, exclude_conversation=conv.id)
    # Память беседы: первое сообщение (если это не оно само) + последняя сводка.
    memory = chat_summary.memory(session, conv.id, req.message)
    # Книга рецептов: модель узнаёт названное пользователем блюдо, а совпавшее по названию
    # блюдо сразу получает готовый рецепт семьи (без генерации).
    book = book_index(session)
    plan_id = uuid4().hex
    set_ai_context(conversation_id=conv.id, plan_id=plan_id, endpoint="chat_stream")

    dishes: list[dict] = []
    leftovers: list[str] = []
    title = "План на неделю"
    reply = "Готово — вот план на неделю."
    week = ""  # заполнится из события meta (оно всегда раньше блюд)
    provider = ""

    err_msg = ""
    try:
        async for kind, payload in generate_plan_stream(
            req.message, avoid, req.dishes_count, req.gender, req.recipe_model, context=memory,
            book=book_names(session, index=book), user_text=_user_said(session, conv.id),
        ):
            if kind == "meta":
                title, week, reply = payload["title"], payload["week_label"], payload["reply"]
                provider = payload.get("provider", "")
                yield ServerSentEvent(
                    event="meta",
                    data={
                        "conversationId": conv.id,
                        "planId": plan_id,
                        "title": title,
                        "weekLabel": week,
                        "reply": reply,
                        "provider": provider,
                    },
                )
            elif kind == "leftovers":
                leftovers = payload
            elif kind == "reply":  # реплику поправили (выдуманные остатки) — её и сохраним
                reply = payload
            elif kind == "dish":
                payload = attach(payload, book)
                dishes.append(payload)
                yield ServerSentEvent(
                    event="dish",
                    data=Dish.model_validate(payload).model_dump(by_alias=True),
                )
    except AIError as exc:  # лимит/недоступность модели — покажем понятный текст
        err_msg = str(exc)
    except Exception as exc:  # noqa: BLE001 — сохраняем то, что успели собрать
        logger.warning("chat_stream оборвался: %s", str(exc)[:150])

    if not dishes:
        # conversationId — чтобы «Переотправить» повторил запрос в этой же беседе (у нового чата
        # meta не пришла, и фронт id беседы не знает), а не завёл ещё одну.
        yield ServerSentEvent(
            event="error",
            data={"message": err_msg or "Не удалось составить план", "conversationId": conv.id},
        )
        return

    planstore.new_row(
        session,
        id=plan_id,
        conversation_id=conv.id,
        title=title,
        week_label=week or _week_label(),
        status="draft",
        provider=provider,
        # Блюда из книги рецептов уже с ингредиентами — uses по ним, а не по плану модели.
        dishes=[sync_uses(d, leftovers) for d in dishes],
        leftovers=leftovers or None,
    )
    msg_id = uuid4().hex
    session.add(
        MessageRow(
            id=msg_id,
            conversation_id=conv.id,
            role="assistant",
            text=reply,
            plan_id=plan_id,
            model=req.recipe_model,
        )
    )
    session.commit()
    record_plan("create")
    yield ServerSentEvent(
        event="done",
        data={
            "planId": plan_id,
            "dishesCount": len(dishes),
            "messageId": msg_id,
            "model": req.recipe_model,
            "leftovers": leftovers,
            "reply": reply,  # итоговая реплика (могла измениться после meta)
        },
    )


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep) -> ChatResponse:
    req.recipe_model = resolve_key(req.recipe_model, "chat")  # пусто → дефолт «Чат и план»
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        conv = Conversation(id=uuid4().hex)
        session.add(conv)
        session.commit()
        record_conversation()

    set_ai_context(conversation_id=conv.id, endpoint="chat")  # до фоновых задач (см. выше)
    if _save_user_message(session, conv.id, req.message, resend=req.resend):
        prefs.learn_async(req.message)  # фоново запоминаем предпочтения (CF, бесплатно)
        chat_summary.schedule(conv.id)  # сводка беседы — фоном, дебаунс 5 с
    avoid = variety_avoid(session, exclude_conversation=conv.id)
    memory = chat_summary.memory(session, conv.id, req.message)
    book = book_index(session)
    set_ai_context(conversation_id=conv.id, endpoint="chat")

    try:
        data = await generate_plan(
            req.message, avoid, req.dishes_count, req.gender, req.recipe_model, context=memory,
            book=book_names(session, index=book), user_text=_user_said(session, conv.id),
        )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Генерация недоступна: {exc}") from exc

    plan_row = planstore.new_row(
        session,
        id=uuid4().hex,
        conversation_id=conv.id,
        title=data["title"],
        week_label=data["week_label"],
        status="draft",
        provider=data.get("provider", ""),
        dishes=[sync_uses(d, data.get("leftovers")) for d in attach_all(data["dishes"], book)],
        leftovers=data.get("leftovers") or None,
    )
    chat_msg_id = uuid4().hex
    session.add(
        MessageRow(
            id=chat_msg_id,
            conversation_id=conv.id,
            role="assistant",
            text=data["reply"],
            plan_id=plan_row.id,
            model=req.recipe_model,
        )
    )
    session.commit()
    session.refresh(plan_row)
    record_plan("create")

    return ChatResponse(
        conversation_id=conv.id,
        reply=data["reply"],
        plan=to_week_plan(plan_row),
        message_id=chat_msg_id,
        model=req.recipe_model,
    )


def _latest_plan(
    session: Session, conversation_id: str, *, with_library: bool = False
) -> PlanRow | None:
    """Последняя версия плана беседы. Служебный план «Мои рецепты» (status library) — не план
    недели: правка в чате его не берёт (with_library — только для ссылок обсуждения)."""
    q = select(PlanRow).where(PlanRow.conversation_id == conversation_id)
    if not with_library:
        q = q.where(PlanRow.status != LIBRARY_STATUS)
    return session.exec(q.order_by(PlanRow.created_at.desc())).first()


def _save_user_message(session: Session, conversation_id: str, text: str, *, resend: bool) -> bool:
    """Реплика пользователя в историю беседы. Повтор упавшего запроса («Переотправить») её второй
    раз не пишет — она уже последняя в беседе. True — записали новую."""
    if resend and is_repeat(session, conversation_id, text):
        return False
    session.add(MessageRow(id=uuid4().hex, conversation_id=conversation_id, role="user", text=text))
    session.commit()
    return True


def _user_said(session: Session, conversation_id: str) -> str:
    """Всё, что пользователь сам написал в беседе: остатки плана принимаем, только если продукт
    есть здесь (planner._ground_plan) — модель не выдумывает их из «недавно ели» и книги."""
    texts = session.exec(
        select(MessageRow.text).where(
            MessageRow.conversation_id == conversation_id, MessageRow.role == "user"
        )
    ).all()
    return "\n".join(t for t in texts if t)


def _edit_context(session: Session, conversation_id: str, current_text: str, max_recent: int = 2) -> str:
    """Узкий контекст для правки плана: первое сообщение + сводка беседы + последние реплики.

    Даёт модели понять, какое блюдо имеется в виду (напр. «один суп» → изначально куриный),
    не пересобирая весь чат: давнее — в сводке (services/summary), свежее — последними репликами.
    Текущее сообщение (последняя user-реплика) в контекст не включаем — оно уже «Просьба»."""
    msgs = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at)
    ).all()
    # Реплики обсуждения рецепта/готовки/покупок к правке плана не относятся — мимо контекста.
    msgs = [m for m in msgs if not m.discuss_target]
    if not msgs:
        return ""
    original = next((m for m in msgs if m.role == "user" and m.text.strip()), None)
    # Хвост без текущей user-реплики (её текст == current_text).
    tail = msgs[:-1] if msgs[-1].role == "user" and msgs[-1].text == current_text else msgs
    recent = [m for m in tail[-max_recent:] if m is not original and m.text.strip()]
    conv = session.get(Conversation, conversation_id)
    memory = chat_memory_block(original.text if original else "", (conv.summary or "") if conv else "")
    parts: list[str] = [memory] if memory else []
    if recent:
        hist = "\n".join(
            f"{'Пользователь' if m.role == 'user' else 'Ты'}: {m.text.strip()}" for m in recent
        )
        parts.append("Недавно в диалоге:\n" + hist)
    return "\n".join(parts)


@router.post("/chat/edit")
async def chat_edit(req: ChatRequest, session: SessionDep) -> ChatResponse:
    """Правка текущего плана диалога через function calling (добавить/убрать/заменить блюдо
    или пересобрать меню). Обновляет существующий план на месте, а не создаёт новый."""
    req.recipe_model = resolve_key(req.recipe_model, "chat")  # пусто → дефолт «Чат и план»
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")

    row = _latest_plan(session, conv.id)  # служебный план «Мои рецепты» правка не берёт
    if conv.id == LIBRARY_ID and row is None:
        # Беседа «Моих рецептов» — обсуждения своих рецептов, плана недели в ней нет: ниже
        # правка собрала бы тут новый план (напр. «Заменить блюдо» из обсуждения своего
        # рецепта). Черновик, составленный в этой беседе обычным сообщением, правится как любой.
        raise HTTPException(status_code=409, detail="«Мои рецепты» — не план недели")
    button = bool(req.remove_dish_id or req.replace_dish_id or req.add_dish)
    # Плана ещё нет или последний отклонён пользователем (правка отклоняет только родителя,
    # самая свежая версия — никогда) — вести себя как обычное создание нового плана.
    if row is None or (row.status == "rejected" and not button):
        return await chat(req, session)
    set_ai_context(conversation_id=conv.id, plan_id=row.id, endpoint="chat_edit")

    # Текст пользователя в ленте: для кнопок — понятный лейбл действия.
    user_text = req.message
    if req.replace_dish_id:
        tgt = next(
            (d.get("name") for d in (row.dishes or []) if d.get("id") == req.replace_dish_id),
            None,
        )
        user_text = f"Замена «{tgt}»" if tgt else "Замена блюда"
        if req.message.strip():
            user_text += f": {req.message.strip()}"
    elif req.add_dish:
        user_text = "Добавить блюдо"
        if req.message.strip():
            user_text += f": {req.message.strip()}"

    # Крестик (удаление) — мгновенное действие без реплики: пользовательское сообщение не пишем.
    if not req.remove_dish_id and _save_user_message(
        session, conv.id, user_text, resend=req.resend
    ):
        chat_summary.schedule(conv.id)  # сводка беседы — фоном, дебаунс 5 с

    context = _edit_context(session, conv.id, req.message)
    # Остатки плана: в контекст правки (что куда пристроено) и в новые блюда (непристроенные).
    leftovers = [str(x) for x in (row.leftovers or [])]
    if leftovers:
        context = "\n".join(p for p in (context, leftovers_status(leftovers, row.dishes or [])) if p)
    # Вкусы извлекаем из свободного текста правки в чате. Пожелание к кнопке
    # (replace/add — «без рыбы») — разовое: экстрактор не зовём (раньше так в dislikes навсегда
    # попадала «рыба»), КРОМЕ слов про постоянство («макс не любит тыкву вообще», «никогда»).
    # Текстовые правки («без свинины») разбираем: экстрактору даём структурный хинт + контекст,
    # чтобы «замени на не-суп»/«где суп» не улетали в предпочтения (см. prefs._EXTRACT_SYSTEM).
    if req.message.strip() and not button:
        prefs.learn_async(req.message, "Это правка уже составленного плана.\n" + context)
    elif req.message.strip() and button and prefs.has_permanent_marker(req.message):
        prefs.learn_async(
            req.message, "Это пожелание к замене/добавлению блюда по кнопке.\n" + context
        )
    # Память беседы для add/replace/create: что уже отвергнуто здесь + общая история.
    rejected = conversation_rejected(session, conv.id) if not req.remove_dish_id else []
    avoid = variety_avoid(session, exclude_conversation=conv.id) if not req.remove_dish_id else []
    # Книга рецептов (в т.ч. свои рецепты): «добавь мой рецепт X» → блюдо с тем же названием,
    # attach_all ниже подставит готовый рецепт.
    book = book_names(session) if not req.remove_dish_id else []
    # Снимок блюд родителя ДО вызова модели: по нему узнаем блюда, которые правка не тронула
    # (их в новую версию берём из родителя на момент записи — planstore.carry_current).
    parent_id = row.id
    dishes = list(row.dishes or [])
    snapshot = copy.deepcopy(dishes)
    try:
        if req.remove_dish_id:
            # Крестик — детерминированное удаление, вообще без модели.
            result = remove_dish_by_id(dishes, row.title, req.remove_dish_id)
        elif req.replace_dish_id:
            # Точечная замена по кнопке — минуя тул-коллинг (выбор функции).
            result = await replace_dish_by_id(
                dishes, row.title, req.replace_dish_id, req.message,
                req.gender, req.recipe_model,
                context=context, rejected=rejected, avoid=avoid, leftovers=leftovers, book=book,
            )
        elif req.add_dish:
            # Добавление по кнопке — минуя тул-коллинг.
            result = await add_dish_direct(
                dishes, row.title, req.message, req.gender, req.recipe_model,
                context=context, rejected=rejected, avoid=avoid, leftovers=leftovers, book=book,
            )
        else:
            result = await edit_plan(
                dishes, row.title, req.message, req.gender, req.recipe_model, context,
                avoid=avoid, rejected=rejected, leftovers=leftovers, book=book,
                user_text=_user_said(session, conv.id),
            )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Правка недоступна: {exc}") from exc

    reply = result["reply"]

    # Ничего не изменили (модель не поняла правку) — новую версию не создаём.
    if not result.get("changed"):
        nc_id = uuid4().hex
        session.add(
            MessageRow(
                id=nc_id,
                conversation_id=conv.id,
                role="assistant",
                text=reply,
                model=req.recipe_model,
            )
        )
        session.commit()
        return ChatResponse(
            conversation_id=conv.id,
            reply=reply,
            plan=None,
            message_id=nc_id,
            model=req.recipe_model,
        )

    new_leftovers = result.get("leftovers") or leftovers
    index = book_index(session)
    # Родитель — СЕЙЧАС (дальше до коммита нет await): нетронутые правкой блюда берём отсюда,
    # иначе рецепт, догенерённый в родителе, пока модель правила план, терялся в новой версии.
    parent_now, _ = planstore.read_dishes(session, parent_id)
    carried = planstore.carry_current(snapshot, result["dishes"], parent_now)
    # Свои товары покупок (хлеб, йогурт…) и отметки «куплено» к блюдам не привязаны —
    # переезжают в новую версию как есть, свежими из базы (могли добавить, пока модель правила).
    extras_now, checked_now = session.exec(
        select(PlanRow.shopping_extras, PlanRow.shopping_checked).where(PlanRow.id == parent_id)
    ).one()
    # Правка создаёт НОВУЮ версию плана (копию), исходный план остаётся доступен по ссылке.
    new_plan = planstore.new_row(
        session,
        id=uuid4().hex,
        conversation_id=conv.id,
        title=result["title"],
        week_label=row.week_label,
        # Правка ПРИНЯТОГО плана остаётся принятой (иначе план выпадал из «Принятых»
        # и из истории для разнообразия); правка черновика — черновик.
        status="accepted" if row.status == "accepted" else "draft",
        decided_at=row.decided_at if row.status == "accepted" else None,
        provider=result.get("provider") or row.provider,
        parent_id=parent_id,
        # Новые блюда, совпавшие по названию с книгой рецептов, — сразу с готовым рецептом.
        dishes=[sync_uses(d, new_leftovers) for d in attach_all(carried, index)],
        leftovers=new_leftovers or None,
        shopping_extras=extras_now or None,
        shopping_checked=checked_now or None,  # отметки «куплено» — тоже в новую версию
    )
    # Исходная версия заменена новой — сразу отменяем её (остаётся доступной по ссылке,
    # в истории/при перезагрузке чата свернётся как «отменён»).
    row.status = "rejected"
    row.decided_at = datetime.now(timezone.utc)
    session.add(row)
    # Если исходная версия была «текущим» планом покупок/готовки — текущим становится новая.
    if appstate.get_current_plan() == row.id:
        appstate.set_current_plan(new_plan.id)
    # Крестик — без реплики: сообщение несёт только новую версию плана (пустой текст),
    # чтобы карточка отрисовалась при перезагрузке чата.
    edit_msg_id = uuid4().hex
    session.add(
        MessageRow(
            id=edit_msg_id,
            conversation_id=conv.id,
            role="assistant",
            text="" if req.remove_dish_id else reply,
            plan_id=new_plan.id,
            model=req.recipe_model,
        )
    )
    session.commit()
    session.refresh(new_plan)
    record_plan("edit")

    return ChatResponse(
        conversation_id=conv.id,
        reply=reply,
        plan=to_week_plan(new_plan),
        message_id=edit_msg_id,
        model=req.recipe_model,
    )
