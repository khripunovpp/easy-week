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
    # Полный список блюд плана — как JSON (snake_case, см. schemas.Dish).
    dishes: list = Field(default_factory=list, sa_column=Column(JSON))
    # Кэш нормализованного списка покупок (mistral) + подпись состава.
    shopping_cache: list = Field(default_factory=list, sa_column=Column(JSON))
    shopping_sig: str = ""
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None


class MessageRow(SQLModel, table=True):
    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True, foreign_key="conversation.id")
    role: str  # user | assistant
    text: str = ""
    plan_id: str | None = Field(default=None, foreign_key="planrow.id")
    created_at: datetime = Field(default_factory=_now)
