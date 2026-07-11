import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

logger = logging.getLogger("easy_week.db")

_db_file = Path(settings.db_path)
_db_file.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{_db_file}",
    connect_args={"check_same_thread": False},
)


def _ensure_columns() -> None:
    """SQLite не добавляет новые колонки в существующую таблицу — делаем это сами."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table_name, table in SQLModel.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table_name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {coltype}'))
            logger.info("added column %s.%s (%s)", table_name, col.name, coltype)


def init_db() -> None:
    # Импорт моделей, чтобы таблицы зарегистрировались в метаданных.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _ensure_columns()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
