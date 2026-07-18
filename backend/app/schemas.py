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
    # Варианты рецепта по моделям: активный ключ + список ключей с готовыми вариантами
    # (deepseek/gemini/anthropic/cloudflare). Плоские поля выше = активный вариант.
    active_model: str = ""
    variant_models: list[str] = []


class DishVariant(CamelModel):
    # Один вариант рецепта блюда, сгенерированный конкретной моделью (для сравнения).
    model: str
    provider: str = ""
    ingredients: list[Ingredient] = []
    steps: list[str] = []
    tips: list[str] = []
    note: str = ""


class CookingStep(CamelModel):
    # Один шаг единого плана готовки (по всем блюдам недели).
    order: int
    phase: str = ""
    text: str
    active_min: int = 0
    passive_min: int = 0
    # Каких блюд касается шаг (названия) — пусто, если шаг общий (напр. «помыть овощи»).
    dishes: list[str] = []


class CookingPlanVariant(CamelModel):
    # Один вариант плана готовки, сгенерированный конкретной моделью (для сравнения).
    model: str
    provider: str = ""
    steps: list[CookingStep] = []
    note: str = ""


class CookingPlan(CamelModel):
    # Единый оптимизированный план готовки на всю неделю (активный вариант).
    active_model: str = ""
    variant_models: list[str] = []
    provider: str = ""
    steps: list[CookingStep] = []
    note: str = ""


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
    # Пол ассистента — влияет на род в прозе модели (f — женский, m — мужской).
    gender: str = "f"
    # Модель рецептов, выбранная в чате/профиле: deepseek | gemini | cloudflare.
    # Пусто → дефолт из настроек (recipe_model_default). Без фолбэков между моделями.
    recipe_model: str = ""


class ChatResponse(CamelModel):
    conversation_id: str
    reply: str
    plan: WeekPlan | None = None


class StatusRequest(CamelModel):
    status: str  # accepted | rejected | draft


class DetailRequest(CamelModel):
    # Модель для ленивой догенерации рецепта (та же, что выбрана в чате).
    recipe_model: str = ""
    # open — вернуть активный вариант (сгенерить первый, если деталей ещё нет);
    # select — сделать recipe_model активным (сгенерить его вариант, если ещё нет).
    action: str = "open"


class PreferencesBody(CamelModel):
    # Пищевые предпочтения пользователя: что не любит / любит.
    dislikes: list[str] = []
    likes: list[str] = []
