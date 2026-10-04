"""Книга рецептов семьи: блюда принятых планов с готовым рецептом (то, что видно в режиме
«Рецепты»). Две задачи, обе без модели:

- book_names — названия для промпта плана: модель узнаёт блюдо, которое пользователь назвал сам
  («начинка для Chicken Pod Pa, она уже есть в рецептах»), и пишет его тем же названием;
- attach — блюдо нового плана с таким же названием (history.norm_name; падеж/опечатка — тоже:
  то же число слов и сходство ≥ 0.9) сразу получает готовый рецепт из книги (все варианты по
  моделям) и название из книги, а не генерится заново. ↻ на рецепте — как обычно.
"""

import difflib

from sqlmodel import Session, select

from ..models import FavoriteRecipe, PlanRow
from .history import norm_name
from .variants import dish_variants

# Поля рецепта, которые переносим из книги (шапку — тоже: граммовки посчитаны на её порции).
_RECIPE_FIELDS = (
    "variants", "active_model", "ingredients", "steps", "tips", "detail_provider",
    "detail_generated_at", "storage", "servings", "prep_min", "cook_min",
)


def book_index(session: Session) -> dict[str, dict]:
    """norm_name → блюдо с рецептом: самый свежий принятый план с этим блюдом."""
    plans = session.exec(select(PlanRow).where(PlanRow.status == "accepted")).all()
    plans.sort(key=lambda r: (r.decided_at or r.created_at), reverse=True)
    out: dict[str, dict] = {}
    for row in plans:
        for d in row.dishes or []:
            key = norm_name(str(d.get("name") or ""))
            if key and key not in out and d.get("steps") and d.get("ingredients"):
                out[key] = d
    return out


def book_names(session: Session, cap: int = 40, index: dict[str, dict] | None = None) -> list[str]:
    """Названия книги рецептов для промпта плана: избранное первым, дальше — свежие."""
    index = book_index(session) if index is None else index
    favs = {f.key for f in session.exec(select(FavoriteRecipe)).all()}
    keys = [k for k in index if k in favs] + [k for k in index if k not in favs]
    return [str(index[k].get("name")) for k in keys[:cap]]


# «Начинка для чикен пот пая» ≈ «…пот пай»: модель склоняет название из книги.
_FUZZY = 0.9


def _find(name: str, index: dict[str, dict]) -> dict | None:
    key = norm_name(name)
    if key in index:
        return index[key]
    words = len(key.split())
    best, score = None, _FUZZY
    for k, d in index.items():
        if len(k.split()) != words:
            continue
        r = difflib.SequenceMatcher(None, key, k).ratio()
        if r >= score:
            best, score = d, r
    return best


def attach(dish: dict, index: dict[str, dict]) -> dict:
    """Блюдо без рецепта, совпавшее по названию с книгой, → копия рецепта из книги.
    Остальное блюдо (id, эмодзи, uses — какие остатки в него идут) — из нового плана."""
    if dish.get("steps") or dish.get("variants"):
        return dish
    src = _find(str(dish.get("name") or ""), index)
    if src is None:
        return dish
    copied = {f: src[f] for f in _RECIPE_FIELDS if f in src}
    copied["variants"] = dish_variants(src)  # legacy-деталь без variants — тоже вариант
    return {**dish, **copied, "name": src.get("name") or dish.get("name"), "from_book": True}


def attach_all(dishes: list[dict], index: dict[str, dict]) -> list[dict]:
    return [attach(d, index) for d in dishes]
