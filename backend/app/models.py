from datetime import datetime, timezone

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_now)
    # Сводка беседы (services/summary.py): одна, последняя — перезаписывается после каждой
    # реплики пользователя (дебаунс 5 с); пусто на старте. summary_upto — id последнего
    # сообщения, вошедшего в сводку (дальше — «новые реплики» для следующего обновления).
    summary: str | None = Field(default=None)
    summary_upto: str | None = Field(default=None)
    summary_at: datetime | None = Field(default=None)


class PlanRow(SQLModel, table=True):
    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True, foreign_key="conversation.id")
    title: str
    week_label: str
    # draft | accepted | rejected; library — служебный план «Мои рецепты» (services/recipebook).
    status: str = Field(default="draft", index=True)
    # Какой моделью составлен план (DeepSeek | Cloudflare) — для показа смены модели в чате.
    provider: str = Field(default="")
    # Правка в чате создаёт КОПИЮ плана (новый id), исходный остаётся доступен по ссылке.
    # parent_id указывает на план-предшественник этой версии.
    parent_id: str | None = Field(default=None, index=True)
    # Полный список блюд плана — как JSON (snake_case, см. schemas.Dish). Пишется ТОЛЬКО через
    # services/planstore: точечно по id блюда, с перечитыванием строки прямо перед записью.
    dishes: list = Field(default_factory=list, sa_column=Column(JSON))
    # Счётчик записей dishes для compare-and-swap (planstore.patch_dishes): запись проходит,
    # только если с момента чтения его никто не сдвинул. NULL у старых строк = 0.
    dishes_version: int | None = Field(default=None)
    # Остатки, которые пользователь просил пристроить («остался порей, сельдерей…») — модель
    # плана выделяет их из запроса; у блюда — dish["uses"]. В покупках такие продукты уходят
    # в группу «Есть дома». Правки плана переносят список в новую версию.
    leftovers: list | None = Field(default=None, sa_column=Column(JSON))
    # Кэш нормализованного списка покупок (mistral) + подпись состава.
    shopping_cache: list = Field(default_factory=list, sa_column=Column(JSON))
    shopping_sig: str = ""
    # Когда собран закэшированный список покупок (для подписи «собран …» на странице).
    shopping_at: datetime | None = None
    # Какая модель нормализовала закэшированный список (ключ) — для оценки 👍/👎 покупок.
    shopping_model: str = ""
    # Свои товары мимо рецептов («ещё хлеб, йогурт»): [{id, name, qty, unit, category}] —
    # разобраны моделью из текста пользователя, в список покупок добавляются к продуктам
    # рецептов. Правка плана в чате переносит их в новую версию.
    shopping_extras: list | None = Field(default=None, sa_column=Column(JSON))
    # Кэш единого плана готовки: {"variants": {model: {steps, note, provider}},
    # "active_model": str, "sig": str}. Варианты по моделям — для сравнения. Пишется через
    # planstore.patch_cooking (вариант модели вливается в свежепрочитанный кэш).
    cooking_plan: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None


class MessageRow(SQLModel, table=True):
    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True, foreign_key="conversation.id")
    role: str  # user | assistant
    text: str = ""
    plan_id: str | None = Field(default=None, foreign_key="planrow.id")
    # Ключ модели, сгенерившей ответ (для оценки 👍/👎 у реплик бота). Пусто у user/старых.
    model: str = Field(default="")
    # Обсуждение («💬 Обсудить в чате»): к какой цели относится реплика —
    # recipe | cooking | shopping (пусто у обычных сообщений), и блюдо для recipe.
    # Такие сообщения не создают версий плана; по ним собирается контекст перегенерации.
    discuss_target: str | None = Field(default=None)
    dish_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class RatingRow(SQLModel, table=True):
    """Оценка 👍/👎 сгенерированного моделью ответа. Авторизации нет — одно глобальное
    хранилище; один голос на (target_type, target_id, model), апсерт в роутере."""

    id: str = Field(primary_key=True)
    target_type: str = Field(index=True)  # recipe | plan | cooking | message
    target_id: str = Field(index=True)
    model: str = Field(default="", index=True)  # ключ модели (deepseek|gemini|anthropic|cloudflare)
    vote: int = 0  # 1 | -1
    # Причины 👎 — ключи из services/rating_reasons через запятую; note — текст «Другое».
    reasons: str = Field(default="")
    note: str = Field(default="")
    # Корреляция для анализа (nullable).
    plan_id: str | None = Field(default=None)
    dish_id: str | None = Field(default=None)
    conversation_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class FavoriteRecipe(SQLModel, table=True):
    """Избранный рецепт (звезда в режиме «Рецепты» на странице планов) — общий для семьи.

    Ключ — нормализованное название блюда (history.norm_name): правка плана создаёт новую
    версию с новым id, а звезда остаётся на блюде. plan_id/dish_id — откуда отметили (для
    справки), рецепт открывается из свежего принятого плана с этим блюдом."""

    key: str = Field(primary_key=True)
    name: str
    plan_id: str | None = None
    dish_id: str | None = None
    created_at: datetime = Field(default_factory=_now)


# --- Хранилище рецептов (services/recipestore, миграция app/migrations/recipes_v1) ---
#
# Свой registry → своя MetaData: SQLModel.metadata.create_all в db.init_db эти таблицы НЕ
# создаёт, а _ensure_columns их не трогает. Создаёт их только CLI миграции
# (`python -m app.migrations apply`) при остановленном сервисе — dev-uvicorn или тесты на общей
# с продом базе не меняют её схему при старте. Все колонки — сразу (в т.ч. на будущие фазы:
# hidden_at, gen_id, ctx_uses, source_used): позже меняем таблицы только нумерованными шагами
# миграции (recipes_v1_1, …), а не автодобавлением колонок.
class RecipeStoreModel(SQLModel, registry=registry()):
    pass


RECIPE_METADATA = RecipeStoreModel.metadata

# Ссылки на рецепт/версию у оценок, избранного и реплик обсуждения. Не поля ORM: до миграции
# колонок в базе нет, а ORM выбирал бы их в каждом SELECT. Добавляет миграция (ALTER TABLE),
# пишет services/recipestore (Core UPDATE — только когда миграция применена). Поиск по ним —
# со следующей фазы (сейчас ★ — по названию, 👍/👎 — по блюду и модели, как раньше).
RECIPE_REF_COLUMNS: dict[str, tuple[str, ...]] = {
    "ratingrow": ("recipe_id", "revision_id"),
    "favoriterecipe": ("recipe_id",),
    "messagerow": ("recipe_id",),
}


class Recipe(RecipeStoreModel, table=True):
    """Рецепт блюда как сущность: одна линия версий плана в чате, рецепт из Книги, свой рецепт.
    Здесь нет ничего, что меняется от генерации к генерации, и нет «текущей версии»: какую
    версию показывает план, решают закрепления блюда в плане (dish.rev_ids)."""

    __tablename__ = "recipe"

    # uuid5(NS_RECIPE, key).hex — детерминирован: снять и заново применить миграцию → те же id.
    id: str = Field(primary_key=True)
    # 'lin:<план первого появления>/<dish_id>/<name_key>' | 'own:<dish_id своего рецепта>'.
    # Считается один раз; после закрепления не пересчитывается (удаление корня не делит рецепт).
    key: str = Field(unique=True)
    name: str
    name_key: str = Field(index=True)  # history.norm_name(name)
    emoji: str = "🍽️"
    desc: str = ""  # задумка
    # Исходный текст своего рецепта — неизменен после создания ('' у рецептов планов).
    source: str = ""
    origin: str = Field(index=True)  # plan | own
    # Снимок шапки (для списков без плана): свой рецепт — из «Моих рецептов», рецепт плана —
    # из самого свежего принятого плана с ним (обновляет `migrations sync`).
    servings: int = 4
    prep_min: int = 0
    cook_min: int = 0
    tags: list = Field(default_factory=list, sa_column=Column(JSON))
    storage: dict = Field(default_factory=dict, sa_column=Column(JSON))  # vacuum/freeze/shelf
    conversation_id: str | None = Field(default=None, index=True)  # «домашний» чат
    origin_plan_id: str | None = None
    origin_dish_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class RecipeRevision(RecipeStoreModel, table=True):
    """Одна сгенерированная версия рецепта — неизменна (триггер recipe_revision_immutable).
    Варианты по моделям и откат — закрепления блюда плана (dish.rev_ids) на эти строки."""

    __tablename__ = "recipe_revision"
    # Правило ничьей: тот же текст с другим провайдером/датой/source — отдельная строка, а не
    # «победитель» молча (id — uuid5 от этих же полей).
    __table_args__ = (
        UniqueConstraint("recipe_id", "model", "content_hash", "meta_hash",
                         name="uq_recipe_revision_content"),
    )

    id: str = Field(primary_key=True)  # uuid5(NS_REV, recipe_id|model|content_hash|meta_hash)
    recipe_id: str = Field(index=True)
    model: str = Field(index=True)  # слот варианта = ключ провайдера (deepseek|gemini|…)
    model_ref: str = ""  # точная модель «провайдер:id» (с фазы 0a), '' у старых данных
    provider: str = ""  # подпись detailProvider («Claude», «DeepSeek»)
    servings: int = 4  # на сколько порций посчитаны граммовки
    ingredients: list = Field(default_factory=list, sa_column=Column(JSON))
    steps: list = Field(default_factory=list, sa_column=Column(JSON))
    tips: list = Field(default_factory=list, sa_column=Column(JSON))
    note: str = ""  # заметка о хранении (зеркало storage.note)
    content_hash: str = Field(index=True)  # sha1 канонического [ingredients, steps, tips, note]
    # Текст своего рецепта (с накопленными «Уточнение: …»), по которому писали ('' у планов).
    source_used: str = ""
    meta_hash: str  # sha1(provider|generated_at|source_used)
    # generate | regenerate | chat_edit | discuss_edit | backfill | custom | migrated | resync
    kind: str
    change: str = ""  # «Что учесть?» / правка из чата или обсуждения
    parent_id: str | None = None  # версия, от которой шли
    # Где сгенерировано (у перенесённых — первое появление).
    plan_id: str | None = None
    dish_id: str | None = None
    conversation_id: str | None = None
    ctx_uses: list = Field(default_factory=list, sa_column=Column(JSON))  # остатки плана
    gen_id: str = ""  # id вызова модели в AI-логе (ai-*.jsonl)
    generated_at: str = ""  # ISO-строка ровно как в JSON ('' — неизвестно)
    # Разобранный generated_at, иначе — время самой ранней версии плана с этим текстом.
    created_at: datetime = Field(default_factory=_now, index=True)
    created_at_estimated: bool = False  # дата приблизительная (в истории — «≈»)
    hidden_at: datetime | None = None  # зарезервировано: «Скрыть версию»


class SchemaMigration(RecipeStoreModel, table=True):
    """Маркер применённого шага миграции + журнал (пишет только CLI app.migrations)."""

    __tablename__ = "schema_migration"

    name: str = Field(primary_key=True)  # recipes_v1, позже recipes_v1_1 …
    applied_at: datetime = Field(default_factory=_now)
    app_commit: str = ""
    backup_path: str = ""
    stats: dict = Field(default_factory=dict, sa_column=Column(JSON))
    verify: dict = Field(default_factory=dict, sa_column=Column(JSON))
    verified_at: datetime | None = None
