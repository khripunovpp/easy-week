from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    # Внутри — snake_case; наружу (в API) — camelCase, как в моделях фронта.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Ingredient(CamelModel):
    name: str
    qty: float
    unit: str
    category: str


class Storage(CamelModel):
    vacuum: bool = True
    freeze: bool = True
    shelf_life_days: int
    note: str | None = None


class Dish(CamelModel):
    id: str
    name: str
    emoji: str
    servings: int
    prep_min: int
    cook_min: int
    tags: list[str] = []
    storage: Storage
    tips: list[str] = []
    steps: list[str] = []
    ingredients: list[Ingredient] = []
    # Какой моделью сгенерирован развёрнутый рецепт (пусто, пока деталь не грузили).
    detail_provider: str = ""


class WeekPlan(CamelModel):
    id: str
    conversation_id: str = ""
    title: str
    week_label: str
    status: str
    # Модель, составившая план (DeepSeek | Cloudflare).
    provider: str = ""
    dishes: list[Dish]


class ChatMessageOut(CamelModel):
    id: str
    role: str
    text: str = ""
    plan: WeekPlan | None = None


class PlanSummary(CamelModel):
    id: str
    title: str
    week_label: str
    status: str
    dishes_count: int
    total_cook_min: int
    emoji: str


class ShoppingItem(CamelModel):
    name: str
    qty: float
    unit: str
    category: str


class ShoppingGroup(CamelModel):
    category: str
    items: list[ShoppingItem]


# --- запрос/ответ чата ---


class ChatRequest(CamelModel):
    conversation_id: str | None = None
    message: str
    dishes_count: int = Field(default=5, ge=2, le=12)
    # Если задан — правка = точечная замена этого блюда (кнопка «заменить» в карточке).
    # Бэкенд меняет именно его, без выбора функции моделью.
    replace_dish_id: str | None = None
    # Если задан — детерминированное удаление блюда (крестик): вообще без модели, только в БД.
    remove_dish_id: str | None = None
    # Если true — добавить одно блюдо в текущий план (кнопка «Добавить блюдо»), минуя тул-коллинг.
    add_dish: bool = False


class ChatResponse(CamelModel):
    conversation_id: str
    reply: str
    plan: WeekPlan | None = None


class StatusRequest(CamelModel):
    status: str  # accepted | rejected | draft
