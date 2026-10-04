"""Миграции хранилища — только явным шагом CLI, никогда на старте приложения.

    cd backend && .venv/bin/python -m app.migrations <команда> [--db ПУТЬ | --live]

Команды (подробно — deploy/README.md «Миграции»): status, rehearse, apply, verify, sync,
strip, drop. Шаг сейчас один — recipes_v1 (таблицы рецептов, app/migrations/recipes_v1.py).

Почему не на старте: рабочая копия на Пае — она же продакшен (backend/data — живая база), и
dev-uvicorn или тесты на ней не должны менять схему. Поэтому:
- без --live CLI не трогает живую базу вообще: --db на её файл — отказ, из какого бы каталога
  ни запускали (живая база — по unit-файлу сервиса и по каталогу бэкенда, где лежит файл, а не
  только по .env текущего каталога: рядом с продом лежат рабочие копии агентов);
- пишущие команды с --live — только при остановленном сервисе (systemctl is-active), и любые
  пишущие — только если файл базы не открыт другим процессом (/proc/*/fd);
- apply сначала делает свою копию базы (online-backup API) и integrity_check, всё меняет одной
  транзакцией и коммитит только после сверки — при любом сбое база логически прежняя (данные и
  схема; байты свободных страниц SQLite после отката могут отличаться — их не журналирует).

Здесь — общее для шагов: движок с честными транзакциями SQLite, бэкап, пути, проверка сервиса.
"""

import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import create_engine

from ..config import settings

logger = logging.getLogger("easy_week.migrations")

SERVICE = "easy-week-backend"
BACKUP_DIRNAME = "backups"
# Своих копий «перед <шагом>» храним по KEEP_BACKUPS на КАЖДЫЙ шаг: копии перед ежедеплойным
# sync не вытесняют единственную копию перед самой миграцией (точка отката L4).
KEEP_BACKUPS = 10

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
# = config.Settings.db_path по умолчанию (без DB_PATH в окружении и .env).
DEFAULT_DB = "data/easy_week.db"
# Где systemd держит unit сервиса (+ копия в репо — если unit не установлен или читается иначе).
UNIT_DIRS = (Path("/etc/systemd/system"), Path("/usr/lib/systemd/system"),
             Path("/lib/systemd/system"))


class MigrationError(RuntimeError):
    """Миграция отказалась или не прошла сверку — база не изменена."""


def live_db_path() -> Path:
    """Живая база — как у приложения: settings.db_path (из backend/.env) от рабочего каталога."""
    return Path(settings.db_path).expanduser().resolve()


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


def _env_db(env_file: Path) -> str | None:
    """DB_PATH из .env (как pydantic-settings: имя без учёта регистра)."""
    try:
        values = dotenv_values(env_file) if env_file.is_file() else {}
    except (OSError, UnicodeError):
        return None
    return next((v for k, v in values.items() if k.upper() == "DB_PATH" and v), None)


def checkout_db(backend_dir: Path) -> Path:
    """База приложения, запущенного из каталога backend_dir: DB_PATH из его .env, иначе
    умолчание — относительно этого каталога (так её откроет uvicorn оттуда)."""
    db = Path(_env_db(backend_dir / ".env") or DEFAULT_DB).expanduser()
    return (db if db.is_absolute() else backend_dir / db).resolve()


def _unit_settings(unit: Path) -> dict[str, list[str]]:
    """Ключи [Service] unit-файла и его drop-in'ов (*.conf), по порядку присваивания."""
    out: dict[str, list[str]] = {}
    files = [unit, *sorted(Path(f"{unit}.d").glob("*.conf"))]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line or line[0] in "#;[" or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def service_db_paths(unit_name: str = SERVICE) -> list[Path]:
    """Базы сервиса по его unit-файлу (установленному и копии в deploy/): WorkingDirectory +
    DB_PATH из Environment= / EnvironmentFile= / .env рабочего каталога / умолчание. Без вызова
    systemctl — читаем файлы (работает и без systemd, и в тестах)."""
    units = [d / f"{unit_name}.service" for d in UNIT_DIRS]
    units.append(BACKEND_DIR.parent / "deploy" / f"{unit_name}.service")
    out: list[Path] = []
    for unit in units:
        if not unit.is_file():
            continue
        cfg = _unit_settings(unit)
        wd = (cfg.get("WorkingDirectory") or [""])[-1].lstrip("-")
        if not wd.startswith("/"):
            continue
        db = None
        for env in cfg.get("Environment", []):
            for item in shlex.split(env):
                k, _, v = item.partition("=")
                if k.upper() == "DB_PATH" and v:
                    db = v
        if db is None:
            for env_file in cfg.get("EnvironmentFile", []):
                db = _env_db(Path(env_file.lstrip("-"))) or db
        base = Path(wd)
        path = Path(db).expanduser() if db else checkout_db(base)
        path = (path if path.is_absolute() else base / path).resolve()
        if path not in out:
            out.append(path)
    return out


def live_db_reason(path: Path) -> str | None:
    """Это чья-то живая база? Причина (для отказа) или None. Проверяем: .env текущего каталога
    (live_db_path), unit сервиса, и каталоги вверх от файла — каталог бэкенда (app/ или .env),
    чья база — этот файл: живая база другой рабочей копии тоже не цель для --db."""
    if same_file(path, live_db_path()):
        return "DB_PATH текущего каталога"
    for p in service_db_paths():
        if same_file(path, p):
            return f"база сервиса {SERVICE}"
    for d in path.parents:
        if ((d / "app" / "main.py").is_file() or (d / ".env").is_file()) and same_file(
                path, checkout_db(d)):
            return f"база приложения из {d}"
    return None


def open_by_others(path: Path) -> list[int]:
    """Процессы (кроме нашего), у которых открыт файл базы или её журнал: обход /proc/*/fd.
    Работающий сервис держит соединение открытым (пул SQLAlchemy) — писать под ним нельзя."""
    want = set()
    for p in (path, Path(f"{path}-journal"), Path(f"{path}-wal")):
        try:
            st = p.stat()
        except OSError:
            continue
        want.add((st.st_dev, st.st_ino))
    pids: list[int] = []
    if not want:
        return pids
    me = os.getpid()
    try:
        procs = list(os.scandir("/proc"))
    except OSError:
        return pids
    for proc in procs:
        if not proc.name.isdigit() or int(proc.name) == me:
            continue
        try:
            fds = list(os.scandir(f"/proc/{proc.name}/fd"))
        except OSError:  # чужой пользователь / процесс уже завершился
            continue
        for fd in fds:
            try:
                st = os.stat(fd.path)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in want:
                pids.append(int(proc.name))
                break
    return pids


def hot_journal_error(path: Path, exc: BaseException) -> MigrationError | None:
    """Чтение (mode=ro) упёрлось в горячий журнал прерванной записи: откатить его можно только
    открыв базу на запись. Понятная ошибка вместо трассировки — или None (другая ошибка)."""
    orig = getattr(exc, "orig", None) or exc
    if getattr(orig, "sqlite_errorname", "") != "SQLITE_READONLY_ROLLBACK":
        return None
    return MigrationError(
        f"{path}: горячий журнал {path.name}-journal после прерванной записи — на чтение базу "
        f"не открыть. Запустите сервис (он откатит журнал при первом обращении) или откройте "
        f"базу на запись любым клиентом SQLite и повторите"
    )


def service_active(unit: str = SERVICE) -> bool:
    """Сервис запущен? Не смогли узнать (нет systemctl) — считаем запущенным: пишущая команда
    на живой базе откажется (явный обход — --assume-stopped)."""
    try:
        out = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("не удалось спросить systemctl про %s: %s", unit, exc)
        return True
    return out.stdout.strip() in ("active", "activating", "reloading", "deactivating")


def open_engine(path: Path, *, readonly: bool = False) -> Engine:
    """Движок на файл базы с честными транзакциями: pysqlite сам не открывает транзакцию перед
    DDL и SAVEPOINT, поэтому управление транзакцией у нас — BEGIN IMMEDIATE (запись) / BEGIN
    (чтение) в начале каждой. Так CREATE TABLE, ALTER TABLE и все записи — одна транзакция,
    и ROLLBACK возвращает прежние данные и схему (байты переиспользованных свободных страниц
    SQLite не журналирует — они могут остаться другими). readonly — файл открыт mode=ro."""
    url = (f"sqlite:///file:{path}?mode=ro&uri=true" if readonly else f"sqlite:///{path}")
    eng = create_engine(url, poolclass=NullPool, connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _manual_tx(dbapi_conn, _):
        dbapi_conn.isolation_level = None

    @event.listens_for(eng, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN" if readonly else "BEGIN IMMEDIATE")

    return eng


def copy_db(src: Path, dst: Path) -> Path:
    """Консистентная копия базы через online-backup API; источник открыт ТОЛЬКО на чтение."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)
    except sqlite3.OperationalError as exc:
        raise hot_journal_error(src, exc) or exc
    finally:
        d.close()
        s.close()
    return dst


def integrity_ok(path: Path) -> str:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(c.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.OperationalError as exc:
        raise hot_journal_error(path, exc) or exc
    finally:
        c.close()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def make_backup(path: Path, step: str) -> Path:
    """Своя копия базы перед записью: backups/easy_week-pre-<шаг>-<время>.db рядом с базой +
    integrity_check копии. Не вышло — MigrationError (мигрировать без точки отката нельзя)."""
    out = path.parent / BACKUP_DIRNAME / f"{path.stem}-pre-{step}-{stamp()}.db"
    try:
        copy_db(path, out)
    except sqlite3.Error as exc:
        raise MigrationError(f"не удалось сделать копию базы {out}: {exc}") from exc
    check = integrity_ok(out)
    if check != "ok":
        raise MigrationError(f"копия {out} не прошла integrity_check: {check}")
    _prune(out.parent, f"{path.stem}-pre-{step}-")
    logger.info("копия базы перед %s: %s", step, out)
    return out


def _prune(folder: Path, prefix: str) -> None:
    olds = sorted(folder.glob(f"{prefix}*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[KEEP_BACKUPS:]:
        p.unlink(missing_ok=True)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def app_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(BACKEND_DIR), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
