from datetime import datetime, timezone

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_now)


class PlanRow(SQLModel, table=True):
    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True, foreign_key="conversation.id")
    title: str
    week_label: str
    status: str = Field(default="draft", index=True)  # draft | accepted | rejected
    # Какой моделью составлен план (DeepSeek | Cloudflare) — для показа смены модели в чате.
    provider: str = Field(default="")
    # Правка в чате создаёт КОПИЮ плана (новый id), исходный остаётся доступен по ссылке.
    # parent_id указывает на план-предшественник этой версии.
    parent_id: str | None = Field(default=None, index=True)
    # Полный список блюд плана — как JSON (snake_case, см. schemas.Dish).
    dishes: list = Field(default_factory=list, sa_column=Column(JSON))
    # Кэш нормализованного списка покупок (mistral) + подпись состава.
    shopping_cache: list = Field(default_factory=list, sa_column=Column(JSON))
    shopping_sig: str = ""
    # Кэш единого плана готовки: {"variants": {model: {steps, note, provider}},
    # "active_model": str, "sig": str}. Варианты по моделям — для сравнения.
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
    created_at: datetime = Field(default_factory=_now)


class RatingRow(SQLModel, table=True):
    """Оценка 👍/👎 сгенерированного моделью ответа. Авторизации нет — одно глобальное
    хранилище; один голос на (target_type, target_id, model), апсерт в роутере."""

    id: str = Field(primary_key=True)
    target_type: str = Field(index=True)  # recipe | plan | cooking | message
    target_id: str = Field(index=True)
    model: str = Field(default="", index=True)  # ключ модели (deepseek|gemini|anthropic|cloudflare)
    vote: int = 0  # 1 | -1
    note: str = Field(default="")
    # Корреляция для анализа (nullable).
    plan_id: str | None = Field(default=None)
    dish_id: str | None = Field(default=None)
    conversation_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
