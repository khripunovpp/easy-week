from collections.abc import Generator
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from .config import settings

_db_file = Path(settings.db_path)
_db_file.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{_db_file}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # Импорт моделей, чтобы таблицы зарегистрировались в метаданных.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
