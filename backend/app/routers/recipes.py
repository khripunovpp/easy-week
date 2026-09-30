"""Рецепты из принятых планов и избранное — режим «Рецепты» на странице планов.

GET /api/recipes → блюда всех ПРИНЯТЫХ планов (свежие планы первыми, блюда — в порядке плана),
    с флагом favorite. Правка принятого плана создаёт новую принятую версию, а старую отклоняет,
    поэтому «принятые» — это уже последние версии.
PUT /api/recipes/favorite {name, favorite, planId?, dishId?} → звезда по нормализованному
    названию блюда (models.FavoriteRecipe) — общая для семьи и переживает правки плана.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..db import get_session
from ..models import FavoriteRecipe, PlanRow
from ..schemas import FavoriteBody, FavoriteOut, RecipeItem
from ..services.history import norm_name

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
    plans = session.exec(select(PlanRow).where(PlanRow.status == "accepted")).all()
    # Свежие первыми: по дате принятия, иначе по дате создания.
    plans.sort(key=lambda r: (r.decided_at or r.created_at), reverse=True)
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
