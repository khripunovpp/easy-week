from datetime import datetime, timezone

from sqlalchemy import JSON, Column
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
