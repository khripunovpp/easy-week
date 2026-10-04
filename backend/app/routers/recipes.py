"""Рецепты из принятых планов и избранное — режим «Рецепты» на странице планов.

GET /api/recipes → блюда всех ПРИНЯТЫХ планов (свежие планы первыми, блюда — в порядке плана),
    с флагом favorite. Правка принятого плана создаёт новую принятую версию, а старую отклоняет,
    поэтому «принятые» — это уже последние версии.
PUT /api/recipes/favorite {name, favorite, planId?, dishId?} → звезда по нормализованному
    названию блюда (models.FavoriteRecipe) — общая для семьи и переживает правки плана.
POST /api/recipes/improve {text} → «Улучшить»: тот же рецепт понятным текстом (без выдумок).
POST /api/recipes/custom {text} → «Дальше»: полный рецепт строго по тексту → в «Мои рецепты»
    (services/recipebook.library_row) → {planId, dishId} для страницы рецепта.
Свои рецепты — в списке первыми и в книге рецептов (план из чата может их взять).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..ai.base import AIError
from ..ai.limits import LimitError
from ..ai.observe import set_ai_context
from ..ai.planner import generate_custom_recipe, improve_recipe_text
from ..db import get_session
from ..models import FavoriteRecipe, PlanRow
from ..schemas import (
    CustomRecipeOut,
    FavoriteBody,
    FavoriteOut,
    RecipeItem,
    RecipeTextBody,
    RecipeTextOut,
)
from ..services.history import norm_name
from ..services.recipebook import LIBRARY_STATUS, library_row
from ..services.variants import with_detail

router = APIRouter(prefix="/api/recipes", tags=["recipes"])
SessionDep = Annotated[Session, Depends(get_session)]


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@router.get("")
async def list_recipes(session: SessionDep) -> list[RecipeItem]:
    favs = {f.key for f in session.exec(select(FavoriteRecipe)).all()}
    plans = session.exec(
        select(PlanRow).where(PlanRow.status.in_(("accepted", LIBRARY_STATUS)))
    ).all()
    # «Мои рецепты» первыми, дальше свежие планы: по дате принятия, иначе по дате создания.
    plans.sort(key=lambda r: (r.status == LIBRARY_STATUS, r.decided_at or r.created_at),
               reverse=True)
    out: list[RecipeItem] = []
    for row in plans:
        for d in row.dishes or []:
            name = str(d.get("name") or "").strip()
            if not name or not d.get("id"):
                continue
            key = norm_name(name)
            out.append(RecipeItem(
                key=key,
                plan_id=row.id,
                plan_title=row.title,
                week_label=row.week_label,
                plan_decided_at=row.decided_at,
                dish_id=str(d["id"]),
                name=name,
                emoji=str(d.get("emoji") or "🍽️"),
                tags=[str(t) for t in (d.get("tags") or [])][:6],
                prep_min=_int(d.get("prep_min")),
                cook_min=_int(d.get("cook_min")),
                servings=_int(d.get("servings"), 4) or 4,
                has_recipe=bool(d.get("steps")),
                favorite=key in favs,
            ))
    return out


@router.put("/favorite")
async def set_favorite(body: FavoriteBody, session: SessionDep) -> FavoriteOut:
    key = norm_name(body.name)
    row = session.get(FavoriteRecipe, key)
    if body.favorite and row is None:
        session.add(FavoriteRecipe(
            key=key, name=" ".join(body.name.split()), plan_id=body.plan_id, dish_id=body.dish_id,
        ))
        session.commit()
    elif not body.favorite and row is not None:
        session.delete(row)
        session.commit()
    return FavoriteOut(key=key, favorite=body.favorite)


def _ai_http(exc: Exception, what: str) -> HTTPException:
    if isinstance(exc, LimitError):
        return HTTPException(status_code=429, detail=str(exc))
    return HTTPException(status_code=502, detail=f"{what}: {exc}")


@router.post("/improve")
async def improve_recipe(body: RecipeTextBody) -> RecipeTextOut:
    set_ai_context(endpoint="custom_recipe", action="improve")
    try:
        return RecipeTextOut(text=await improve_recipe_text(body.text, body.recipe_model))
    except (LimitError, AIError) as exc:
        raise _ai_http(exc, "Не удалось улучшить текст") from exc


@router.post("/custom")
async def create_custom_recipe(body: RecipeTextBody, session: SessionDep) -> CustomRecipeOut:
    set_ai_context(endpoint="custom_recipe", action="create")
    try:
        head, detail, model_key = await generate_custom_recipe(body.text, body.recipe_model)
    except (LimitError, AIError) as exc:
        raise _ai_http(exc, "Не удалось собрать рецепт") from exc
    row = library_row(session)
    dishes = list(row.dishes or [])
    ids = {d.get("id") for d in dishes}
    base = f"own-{len(dishes)}-" + head["id"].split("-", 2)[-1]
    dish_id, k = base[:48], len(dishes)
    while dish_id in ids:
        k += 1
        dish_id = (f"own-{k}-" + head["id"].split("-", 2)[-1])[:48]
    # source — текст пользователя: перегенерация и другие модели держатся его (prompt._source_block).
    dish = with_detail({**head, "id": dish_id, "source": body.text.strip()}, model_key, detail)
    row.dishes = [*dishes, dish]
    session.add(row)
    session.commit()
    return CustomRecipeOut(plan_id=row.id, dish_id=dish_id)
