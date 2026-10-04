"""Общие фикстуры тестов бэкенда.

БД и AI-логи уводим во временный каталог ДО импорта app (settings читает DB_PATH из env),
чтобы тесты не трогали рабочую data/ и файл предпочтений.

Обе тестовые базы — как прод после деплоя фазы 1: схема приложения + миграция рецептов
(маркер recipes_v1), поэтому все записи блюд в тестах идут с двойной записью в таблицы
рецептов. Поведение без маркера (фаза 0) — свои базы в test_planstore / test_recipestore."""

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


def migrate(engine) -> None:
    """Таблицы рецептов + маркер на пустой (или тестовой) базе — шаг миграции как в CLI."""
    from app.migrations import recipes_v1

    with Session(engine) as s:
        recipes_v1.apply_step(s)
        s.commit()


@pytest.fixture(scope="session", autouse=True)
def _app_db_migrated():
    """Временная файловая база приложения (DB_PATH) — схема + миграция рецептов через CLI."""
    from app.config import settings
    from app.db import init_db
    from app.migrations.__main__ import run_apply

    db_file = Path(settings.db_path).resolve()
    if db_file != Path(os.environ["DB_PATH"]).resolve():
        # app.config импортирован раньше conftest (плагин?) — база не временная: не трогаем.
        pytest.exit(f"тестовая база не временная: {db_file}", returncode=3)
    init_db()
    run_apply(db_file, backup=False)


@pytest.fixture()
def session():
    """Чистая in-memory SQLite на каждый тест (с миграцией рецептов)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    migrate(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _no_prefs(monkeypatch):
    """Предпочтения пользователя в тестах — пустые (не читаем файл)."""
    from app.ai import prefs

    monkeypatch.setattr(prefs, "load", lambda: {"dislikes": [], "likes": []})
