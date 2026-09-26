"""Общие фикстуры тестов бэкенда.

БД и AI-логи уводим во временный каталог ДО импорта app (settings читает DB_PATH из env),
чтобы тесты не трогали рабочую data/ и файл предпочтений."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="easyweek-tests-")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app import models  # noqa: E402,F401 — регистрирует таблицы в метаданных


@pytest.fixture()
def session():
    """Чистая in-memory SQLite на каждый тест."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _no_prefs(monkeypatch):
    """Предпочтения пользователя в тестах — пустые (не читаем файл)."""
    from app.ai import prefs

    monkeypatch.setattr(prefs, "load", lambda: {"dislikes": [], "likes": []})
