from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
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
    # Гарнир к основному блюду из плана (коротко; пусто — не нужен или старые планы).
    garnish: str = ""
    storage: Storage
    tips: list[str] = []
    steps: list[str] = []
    ingredients: list[Ingredient] = []
    # Какой моделью сгенерирован развёрнутый рецепт (пусто, пока деталь не грузили).
    detail_provider: str = ""
    # Когда сгенерирован развёрнутый рецепт (ISO UTC; пусто — старые данные / не грузили).
    detail_generated_at: str = ""
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
    generated_at: str = ""  # когда сгенерирован вариант (ISO UTC)


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
    generated_at: str = ""  # когда сгенерирован вариант (ISO UTC)


class CookingPlan(CamelModel):
    # Единый оптимизированный план готовки на всю неделю (активный вариант).
    active_model: str = ""
    variant_models: list[str] = []
    provider: str = ""
    steps: list[CookingStep] = []
    note: str = ""
    generated_at: str = ""  # когда сгенерирован активный вариант (ISO UTC)


class WeekPlan(CamelModel):
    id: str
    conversation_id: str = ""
    title: str
    week_label: str
    status: str
    # Модель, составившая план (DeepSeek | Cloudflare).
    provider: str = ""
    dishes: list[Dish]
    # Когда создан план и когда собран закэшированный список покупок (для подписей на страницах).
    created_at: datetime | None = None
    shopping_generated_at: datetime | None = None
    shopping_model: str = ""  # ключ модели, собравшей список покупок (для оценки)


class ChatMessageOut(CamelModel):
    id: str
    role: str
    text: str = ""
    plan: WeekPlan | None = None
    model: str = ""
    # Реплика обсуждения («💬 Обсудить в чате»): цель recipe|cooking|shopping, блюдо и план
    # (последняя версия плана беседы) — для ссылки «Открыть рецепт/план готовки/покупки».
    discuss_target: str | None = None
    dish_id: str | None = None
    discuss_plan_id: str | None = None


class RatingBody(CamelModel):
    # Голос 👍/👎 за ответ модели. vote: 1 | -1. Апсерт по (target_type, target_id, model).
    target_type: str  # recipe | plan | cooking | shopping | message
    target_id: str
    model: str = ""
    vote: int
    note: str = ""
    plan_id: str | None = None
    dish_id: str | None = None
    conversation_id: str | None = None


class RatingOut(CamelModel):
    # Текущее состояние голоса цели: 1 | -1 | 0 (нет голоса).
    vote: int = 0
    # До какого момента голос можно менять (ISO UTC); пусто — голоса нет, голосовать можно.
    locks_at: str = ""


class RatingReasonsBody(CamelModel):
    # Причины к уже поставленному 👎 (ключи каталога) + свободный текст к «Другое».
    target_type: str
    target_id: str
    model: str = ""
    reasons: list[str] = []
    note: str = ""


class PlanSummary(CamelModel):
    id: str
    title: str
    week_label: str
    status: str
    dishes_count: int
    total_cook_min: int
    emoji: str
    dish_names: list[str] = []
    created_at: datetime | None = None


class MessageSearchHit(CamelModel):
    # Результат поиска по сообщениям всех бесед: само сообщение + контекст беседы (план).
    id: str
    conversation_id: str
    role: str
    text: str
    plan_title: str | None = None
    plan_emoji: str | None = None


class ModelPrice(CamelModel):
    # Цена модели, USD за 1M токенов (Cloudflare — ещё и за 1000 нейронов).
    input: float = Field(ge=0, le=1000)
    cached_input: float = Field(ge=0, le=1000)
    cache_write: float = Field(ge=0, le=1000)
    output: float = Field(ge=0, le=1000)
    per_1k_neurons: float | None = Field(default=None, ge=0, le=100)


class PricesBody(CamelModel):
    # Цены по ключам моделей (deepseek/gemini/anthropic/cloudflare); неизвестные ключи — 422.
    # Ключ — провайдер (цена по умолчанию) или «провайдер:id» (конкретная модель из каталога);
    # неизвестный ключ — 422 (проверяет роутер).
    prices: dict[str, ModelPrice]


class ShoppingItem(CamelModel):
    name: str
    qty: float
    unit: str
    category: str


class ShoppingGroup(CamelModel):
    category: str
    items: list[ShoppingItem]


class DishShopping(CamelModel):
    # Покупки одного блюда (режим «По рецептам»): детерминированно из его ингредиентов.
    dish_id: str
    name: str
    emoji: str = ""
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
    # Модель, выбранная в чате/профиле: deepseek | gemini | cloudflare | anthropic | openrouter.
    # Пусто → дефолт задачи из настроек. Без фолбэков между моделями.
    recipe_model: str = ""


class ChatResponse(CamelModel):
    conversation_id: str
    reply: str
    plan: WeekPlan | None = None
    message_id: str = ""  # id ассистентского сообщения (для оценки 👍/👎)
    model: str = ""


class StatusRequest(CamelModel):
    status: str  # accepted | rejected | draft


class RenameRequest(CamelModel):
    # Новое название плана (правка пользователем на странице плана)
    title: str = Field(min_length=1, max_length=80)


class DetailRequest(CamelModel):
    # Модель для ленивой догенерации рецепта (та же, что выбрана в чате).
    recipe_model: str = ""
    # open — вернуть активный вариант (сгенерить первый, если деталей ещё нет);
    # select — сделать recipe_model активным (сгенерить его вариант, если ещё нет);
    # regenerate — «↻ Перегенерировать»: всегда новый вариант recipe_model с учётом обсуждения.
    action: str = "open"


class CurrentPlanBody(CamelModel):
    # Выбранный «текущий» план (для покупок/готовки). null — сбросить.
    plan_id: str | None = None


# Пункт предпочтений: непустой, ≤40 символов; списки ≤30 (иначе 422). Лимиты — как в ai/prefs.
PrefItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
PrefList = Annotated[list[PrefItem], Field(max_length=30)]
MacroLevel = Literal["low", "normal", "high"]


class Macros(CamelModel):
    # Акцент БЖУ: меньше / норма / больше. normal везде — в промпт ничего не пишем.
    protein: MacroLevel = "normal"
    fat: MacroLevel = "normal"
    carbs: MacroLevel = "normal"


class MacrosPatch(CamelModel):
    # Частичная правка БЖУ: не переданное остаётся как было.
    protein: MacroLevel | None = None
    fat: MacroLevel | None = None
    carbs: MacroLevel | None = None


class PreferencesBody(CamelModel):
    # PUT /api/preferences — ЧАСТИЧНАЯ замена: None/отсутствует → поле не трогаем
    # (старый клиент шлёт только likes/dislikes и не должен стирать аллергии).
    allergies: PrefList | None = None
    likes: PrefList | None = None
    dislikes: PrefList | None = None
    suggested_allergies: PrefList | None = None
    suggested_dislikes: PrefList | None = None
    suggested_likes: PrefList | None = None
    macros: MacrosPatch | None = None
    diet_note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class PreferencesOut(CamelModel):
    # Полные предпочтения (ответ GET/PUT). Аллергии — жёсткое ограничение, правятся только вручную;
    # suggested_allergies — подозрения экстрактора из чата («Добавить в аллергии?»).
    allergies: list[str] = []
    likes: list[str] = []
    dislikes: list[str] = []
    suggested_allergies: list[str] = []
    # «Похоже на вкус» из чата — подсказки «Добавить?»: в dislikes/likes переносит пользователь.
    suggested_dislikes: list[str] = []
    suggested_likes: list[str] = []
    macros: Macros = Macros()
    diet_note: str = ""


class DiscussRequest(CamelModel):
    # Реплика в режиме «Обсуждение: …» (бейдж в композере). Версий плана не создаёт.
    conversation_id: str | None = None  # пусто → беседа плана (plan.conversation_id)
    plan_id: str
    target: str  # recipe | cooking | shopping
    dish_id: str | None = None  # для target=recipe
    message: str
    recipe_model: str = ""
    gender: str = "f"


class DiscussResponse(CamelModel):
    conversation_id: str
    reply: str
    message_id: str = ""
    model: str = ""
    target: str
    plan_id: str
    dish_id: str | None = None
    # Что применено по явной просьбе: none | edit (рецепт обновлён) | regenerate (пересобрано)
    # | replace (предложена замена блюда — кнопка «Заменить блюдо» на фронте).
    op: str = "none"
    dish: Dish | None = None  # обновлённое блюдо после edit
    cooking: CookingPlan | None = None  # пересобранный план готовки
    shopping: list[ShoppingGroup] | None = None  # пересобранный список покупок
    suggest_replace: bool = False
    replace_query: str = ""
    # Ответ получен, но применить изменение не удалось (модель упала) — текст ошибки.
    apply_error: str = ""


# --- общие настройки (модели по умолчанию по задачам) ---


class ModelDefaults(CamelModel):
    # Модель по умолчанию для каждой задачи. Ключи — как в реестре GATES (ai/gates.py);
    # неизвестный ключ → 422. chat — план/правки/обсуждение, recipe — рецепт блюда
    # (и догенерация для PDF/покупок), shopping — нормализация покупок, cooking — план готовки,
    # prefs — фоновое извлечение предпочтений из чата, summary — фоновая сводка беседы.
    # Подходит ли модель задаче — проверяет роутер по карте services/settings.TASK_MODELS.
    # Значение — ссылка на модель: «провайдер» или «провайдер:id» (services/model_catalog);
    # проверяет роутер (неизвестный провайдер / модель / не для этой задачи → 422).
    chat: str
    recipe: str
    shopping: str
    cooking: str
    prefs: str
    summary: str


class SettingsBody(CamelModel):
    # PUT /api/settings — полный набор моделей по умолчанию.
    models: ModelDefaults


class SettingsOut(CamelModel):
    models: ModelDefaults
    # False — настройки ещё ни разу не сохраняли (отдаём встроенные дефолты); фронт по нему
    # разово переносит старый выбор модели из localStorage (ew.recipeModel).
    initialized: bool = False
    # Конкретные модели за ключами (deepseek → deepseek-chat, gemini → алиас → реальная версия)
    # — для подписей в выпадашках моделей.
    model_names: dict[str, str] = {}
    # Карта «задача → модели, которые можно выбрать» (services/settings.TASK_MODELS): фронт
    # строит по ней выпадашки — неподходящие модели (напр. Cloudflare для рецептов) не предлагает.
    task_models: dict[str, list[str]] = {}
    # Каталог конкретных моделей по провайдерам: {провайдер: [{id, label, note}]} — группы в
    # выпадашках настроек (services/model_catalog).
    catalog: dict[str, list[dict[str, str]]] = {}


# --- рецепты из принятых планов + избранное (режим «Рецепты» на странице планов) ---


class RecipeItem(CamelModel):
    key: str  # нормализованное название — ключ избранного и дедупликации на фронте
    plan_id: str
    plan_title: str
    week_label: str
    plan_decided_at: datetime | None = None
    dish_id: str
    name: str
    emoji: str = "🍽️"
    tags: list[str] = []
    prep_min: int = 0
    cook_min: int = 0
    servings: int = 4
    has_recipe: bool = False  # развёрнутый рецепт уже сгенерирован (есть шаги)
    favorite: bool = False


class FavoriteBody(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    favorite: bool
    plan_id: str | None = None
    dish_id: str | None = None


class FavoriteOut(CamelModel):
    key: str
    favorite: bool
