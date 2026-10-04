"""CLI миграций: `cd backend && .venv/bin/python -m app.migrations <команда> …`.

  status   [--db ПУТЬ | --live]        что применено, сколько рецептов/версий (только чтение)
  rehearse [--db ИСТОЧНИК] [--work DIR] [--keep]
                                       репетиция на копии: apply + сверка, повторный apply
                                       (0 вставок, 0 изменений JSON), drop + apply (те же id);
                                       источник (по умолчанию живая база) открыт ТОЛЬКО на чтение;
                                       копия — в своём новом подкаталоге DIR (по умолчанию
                                       backups/ рядом с базой), удаляется только он
  apply    (--db ПУТЬ | --live) [--no-backup]
                                       своя копия + integrity_check → шаг recipes_v1 одной
                                       транзакцией (сверка до COMMIT, маркер — только при успехе)
                                       → sync
  verify   (--db ПУТЬ | --live) [--against КОПИЯ]
                                       сверка V1–V10 (только чтение); --against — V2 с копией
  sync     (--db ПУТЬ | --live)        догнать JSON (старый код / сбой двойной записи)
  strip    (--db ПУТЬ | --live)        L3: убрать закрепления из JSON (и маркер)
  drop     (--db ПУТЬ | --live)        L3: снять таблицы рецептов и маркер

Живая база (settings.db_path из backend/.env) — только с --live; --db на любую живую базу
(по .env текущего каталога, unit-файлу сервиса или каталогу бэкенда вокруг файла) — отказ.
Пишущие команды с --live — только при остановленном сервисе (`systemctl is-active
easy-week-backend`; без systemd — --assume-stopped), любые пишущие — только если файл не открыт
другим процессом. Коды выхода: 0 — успех; 1 — сбой/сверка (транзакция откачена); 2 — отказ
(аргументы, сервис, файл занят); 3 — apply: шаг миграции применён (сейчас или раньше), а sync не
прошёл (его транзакция откачена, маркер есть).
"""

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from . import (
    BACKUP_DIRNAME,
    MigrationError,
    app_commit,
    copy_db,
    hot_journal_error,
    live_db_path,
    live_db_reason,
    make_backup,
    open_by_others,
    open_engine,
    remove_tree,
    same_file,
    service_active,
    stamp,
)
from . import recipes_v1

logger = logging.getLogger("easy_week.migrations")

WRITE_COMMANDS = {"apply", "sync", "strip", "drop"}


class Refused(MigrationError):
    """Отказ до каких-либо действий (код 2)."""


class SyncFailed(MigrationError):
    """apply: шаг миграции уже закоммичен (сейчас или прошлым деплоем — маркер есть), а sync —
    своя транзакция — не прошёл и откачен (код 3). База не «прежняя»: двойная запись включена."""

    def __init__(self, step_now: bool, cause: BaseException):
        when = "только что применён" if step_now else "применён раньше"
        super().__init__(f"recipes_v1 {when} (маркер есть), sync не прошёл и откачен: {cause}")
        self.step_now = step_now


def _write_report(path: Path, name: str, report: dict) -> Path:
    out = path.parent / BACKUP_DIRNAME / f"{name}-verify-{stamp()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    return out


def _tx(path: Path, fn, *, name: str, readonly: bool = False):
    """fn(session) в ОДНОЙ транзакции: успех — COMMIT, любое исключение — ROLLBACK (данные и
    схема прежние). Провал сверки — отчёт рядом с базой (backups/<name>-verify-*.json)."""
    eng = open_engine(path, readonly=readonly)
    try:
        with Session(eng) as s:
            s.connection()  # BEGIN IMMEDIATE (запись) / BEGIN (чтение)
            try:
                out = fn(s)
            except recipes_v1.VerifyFailed as exc:
                s.rollback()
                rep = _write_report(path, name, exc.report)
                logger.error("%s: сверка не прошла (%s), откат; отчёт: %s", name,
                             ", ".join(exc.report["failed"]), rep)
                raise
            except BaseException:
                s.rollback()
                raise
            if readonly:
                s.rollback()
            else:
                s.commit()
            return out
    except OperationalError as exc:
        # Горячий журнал (база после прерванной записи, а мы — mode=ro): понятная ошибка.
        raise hot_journal_error(path, exc) or exc
    finally:
        eng.dispose()


def run_apply(path: Path, *, backup: bool = True, hook=None) -> dict:
    """Шаги, которых ещё нет (сейчас — recipes_v1), затем sync. Каждый — своей транзакцией."""
    result: dict = {"db": str(path), "backup": ""}
    if backup:
        # Имя копии — по тому, что сейчас будет: сама миграция или только sync.
        pending = run_readonly(path, lambda s: recipes_v1.marker(s) is None)
        result["backup"] = str(make_backup(path, recipes_v1.STEP if pending else "recipes_sync"))
    t0 = time.monotonic()
    commit = app_commit()  # git — до транзакции, чтобы не держать базу под записью

    def step(s: Session):
        if recipes_v1.marker(s) is not None:
            return None
        return recipes_v1.apply_step(s, backup_path=result["backup"], commit=commit, hook=hook)

    result["step"] = _tx(path, step, name=recipes_v1.STEP)
    try:
        result["sync"] = _tx(path, lambda s: recipes_v1.sync_session(s), name="recipes_sync")
    except Exception as exc:
        # Шаг уже закоммичен (или был раньше): «база не изменена» здесь неправда — свой код.
        raise SyncFailed(result["step"] is not None, exc) from exc
    result["seconds"] = round(time.monotonic() - t0, 2)
    return result


def run_readonly(path: Path, fn):
    return _tx(path, fn, name="readonly", readonly=True)


def _snapshot(path: Path) -> tuple[dict, dict]:
    def snap(s: Session):
        return recipes_v1.pin_map(s), recipes_v1.id_sets(s)

    return run_readonly(path, snap)


def run_rehearse(source: Path, work: Path | None = None, *, keep: bool = False) -> dict:
    """Репетиция на копии (источник открыт только на чтение). Падение любой проверки —
    MigrationError, копия остаётся для разбора; успех — копия удаляется (если не --keep).

    Копия — в СВОЁМ новом подкаталоге work (по умолчанию backups/ рядом с базой), и удаляем
    только его: work задаёт человек — это может быть каталог с чужими файлами, каталог самой
    базы или выше (rmtree по нему снёс бы живую базу и бэкапы)."""
    base = work or source.parent / BACKUP_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"rehearse-{stamp()}-", dir=base))
    target = tmp / source.name
    if same_file(target, source):  # каталог новый — не бывает; страховка от копии «в себя»
        raise MigrationError(f"копия репетиции {target} совпадает с источником")
    try:
        copy = copy_db(source, target)
    except BaseException:
        remove_tree(tmp)
        raise
    report: dict = {"source": str(source), "copy": str(copy), "checks": {}, "timing": {}}
    t = time.monotonic()
    was_migrated = run_readonly(copy, lambda s: recipes_v1.marker(s) is not None)
    report["source_migrated"] = was_migrated

    def timed(name: str, fn):
        t0 = time.monotonic()
        out = fn()
        report["timing"][name] = round(time.monotonic() - t0, 2)
        return out

    first = timed("apply", lambda: run_apply(copy, backup=False))
    report["apply"] = first
    pins1, ids1 = _snapshot(copy)

    second = timed("apply_again", lambda: run_apply(copy, backup=False))
    sync2 = second["sync"]
    idem = {k: sync2[k] for k in ("revisions_new", "recipes_new", "rows_changed",
                                  "headers_refreshed")}
    idem["refs_filled"] = sum((sync2.get("refs_filled") or {}).values())
    idem["step_skipped"] = second["step"] is None
    report["checks"]["idempotent"] = {
        "ok": idem["step_skipped"] and not any(v for k, v in idem.items() if k != "step_skipped"),
        **idem,
    }

    # drop без strip: закрепления в JSON остаются → apply восстанавливает ровно те же id.
    timed("drop", lambda: _tx(copy, recipes_v1.drop_session, name="drop"))
    timed("reapply", lambda: run_apply(copy, backup=False))
    pins2, ids2 = _snapshot(copy)
    pinned_ids = lambda pins: (  # noqa: E731
        {r for r, _ in pins.values()}, {v for _, revs in pins.values() for _, v in revs})
    report["checks"]["drop_apply_same_ids"] = {
        "ok": pins2 == pins1 and pinned_ids(pins2) == pinned_ids(pins1)
        and (ids2 == ids1 if not was_migrated else True),
        "pins": len(pins1), "recipes": len(ids2["recipes"]), "revisions": len(ids2["revisions"]),
        "table_only_revisions_lost": len(ids1["revisions"] - ids2["revisions"]),
    }

    # strip + drop + apply: всё заново из JSON. На ещё не мигрированной базе — те же id;
    # на мигрированной расхождение возможно (удалённый корень линии) — только отчёт.
    timed("strip", lambda: _tx(copy, recipes_v1.strip_session, name="strip"))
    timed("drop2", lambda: _tx(copy, recipes_v1.drop_session, name="drop"))
    timed("apply_from_scratch", lambda: run_apply(copy, backup=False))
    pins3, ids3 = _snapshot(copy)
    same = pins3 == pins1
    report["checks"]["strip_drop_apply_same_ids"] = {
        "ok": same if not was_migrated else True, "same": same,
        "differing_pins": sum(1 for k in set(pins1) | set(pins3) if pins1.get(k) != pins3.get(k)),
    }
    report["timing"]["total"] = round(time.monotonic() - t, 2)
    report["ok"] = all(c["ok"] for c in report["checks"].values())
    if not report["ok"]:
        raise MigrationError("репетиция: " + json.dumps(report["checks"], ensure_ascii=False))
    if not keep:
        remove_tree(tmp)
        report["copy"] += " (удалена)"
    return report


# --- вывод ---


def _print_apply(res: dict) -> None:
    step = res.get("step")
    if step is None:
        print("recipes_v1: уже применён")
    else:
        st = step["stats"]
        refs = st["refs"]
        print(f"recipes_v1: {st['recipes']} рецептов ({st['own']} своих), {st['revisions']} "
              f"версий {st['by_model']}, без даты {st['estimated']}")
        print(f"  блюд: {st['dishes']} в {st['planrows']} версиях планов — закреплено "
              f"{st['pinned']}, своё в плане (спека) {st['plan_owned']}, только шапка "
              f"{st['header_only']}; копий из Книги связано {st['book_copies_linked']}")
        print(f"  видимых в Книге {st['visible']}; ссылки: оценки {refs['ratings']} (без версии — "
              f"текст перезаписан ↻: {refs.get('overwritten', 0)}), избранное "
              f"{refs['favorites']}, реплики {refs['messages']}, не разрешилось "
              f"{refs['unresolved']}; {st['seconds']} с")
        _print_verify(step["verify"])
    sync = res.get("sync") or {}
    print(f"sync: +{sync.get('revisions_new', 0)} версий, {sync.get('repinned', 0)} "
          f"перезакреплений, {sync.get('linked', 0)} новых закреплений, "
          f"+{sync.get('recipes_new', 0)} рецептов; {sync.get('seconds', 0)} с")
    if res.get("backup"):
        print(f"копия перед миграцией: {res['backup']}")


def _print_verify(rep: dict) -> None:
    print("сверка: " + ("OK" if rep["ok"] else "НЕ ПРОШЛА: " + ", ".join(rep["failed"])))
    for name, c in rep["checks"].items():
        mark = "ok" if c["ok"] else "FAIL"
        extra = f" {c['actual']}" if c.get("actual") not in (None, "") else ""
        print(f"  {name}: {mark}{extra}")
        for d in c["diffs"][:5]:
            print(f"     - {d}")


def _print_status(res: dict) -> None:
    m = res.get("marker")
    if m:
        print(f"recipes_v1: применён {m['applied_at']} (коммит {m['app_commit'][:8] or '?'}), "
              f"сверка {'OK' if m['verify_ok'] else 'НЕ ПРОШЛА ' + str(m['verify_failed'])} "
              f"{m['verified_at']}")
    else:
        print("recipes_v1: НЕ применён (маркера нет — приложение работает как фаза 0)")
    if "recipes" in res:
        print(f"  рецептов {res['recipes']}, версий {res['revisions']}")
    print(f"  версий планов {res['planrows']}, блюд с рецептом {res['dishes_with_recipe']}, "
          f"закреплено {res['dishes_pinned']}")


def _print(cmd: str, res, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        return
    if cmd == "apply":
        _print_apply(res)
    elif cmd == "verify":
        _print_verify(res)
    elif cmd == "status":
        _print_status(res)
    elif cmd == "rehearse":
        print(f"репетиция на копии {res['copy']} (источник {res['source']}, "
              f"{'уже мигрирован' if res['source_migrated'] else 'ещё не мигрирован'})")
        _print_apply(res["apply"])
        for name, c in res["checks"].items():
            print(f"  {name}: {'ok' if c['ok'] else 'FAIL'} "
                  f"{ {k: v for k, v in c.items() if k != 'ok'} }")
        print(f"  время: {res['timing']}")
    else:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


# --- разбор аргументов и защита живой базы ---


def _target(args, *, is_active) -> Path:
    if args.live and args.db:
        raise Refused("либо --db ПУТЬ, либо --live")
    if args.live:
        path = live_db_path()
        if args.cmd in WRITE_COMMANDS and not args.assume_stopped and is_active():
            raise Refused("сервис easy-week-backend запущен — сначала "
                          "`sudo systemctl stop easy-week-backend` (миграцию живой базы делает "
                          "deploy/update.sh)")
    elif args.db:
        path = Path(args.db).expanduser().resolve()
        # Не только .env текущего каталога: из соседней рабочей копии (агенты работают в
        # worktree рядом с продом) «живая база» по cwd — другой файл.
        why = live_db_reason(path)
        if why:
            raise Refused(f"{path} — живая база ({why}): только с --live из её каталога "
                          f"backend, при остановленном сервисе")
    else:
        raise Refused("нужен --db ПУТЬ (копия) или --live (живая база)")
    if not path.exists():
        raise Refused(f"нет файла базы {path}")
    if args.cmd in WRITE_COMMANDS:
        # Файл держит другой процесс (сервис, dev-uvicorn, sqlite3) — писать под ним нельзя.
        pids = open_by_others(path)
        if pids:
            raise Refused(f"{path} открыт другими процессами (pid {', '.join(map(str, pids))})"
                          f" — остановите их и повторите")
    return path


def main(argv: list[str] | None = None, *, is_active=service_active) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.migrations",
                                description="Миграции хранилища Easy Week")
    p.add_argument("cmd", choices=["status", "rehearse", "apply", "verify", "sync", "strip",
                                   "drop"])
    p.add_argument("--db", help="файл базы (копия); для rehearse — источник")
    p.add_argument("--live", action="store_true", help="живая база (settings.db_path)")
    p.add_argument("--assume-stopped", action="store_true",
                   help="без systemd: подтверждаю, что приложение остановлено")
    p.add_argument("--no-backup", action="store_true", help="apply без своей копии (копии)")
    p.add_argument("--against", help="verify: V2 против этой копии базы")
    p.add_argument("--work", help="rehearse: каталог копии")
    p.add_argument("--keep", action="store_true", help="rehearse: не удалять копию")
    p.add_argument("--json", action="store_true", help="полный отчёт JSON")
    args = p.parse_args(argv)

    try:
        if args.cmd == "rehearse":
            if args.live:
                raise Refused("rehearse всегда работает на копии; источник — --db или живая база")
            source = Path(args.db).expanduser().resolve() if args.db else live_db_path()
            if not source.exists():
                raise Refused(f"нет файла базы {source}")
            res = run_rehearse(source, Path(args.work).resolve() if args.work else None,
                               keep=args.keep)
        else:
            path = _target(args, is_active=is_active)
            if args.cmd == "apply":
                if args.live and args.no_backup:
                    raise Refused("живую базу без своей копии не мигрируем")
                res = run_apply(path, backup=not args.no_backup)
            elif args.cmd == "status":
                res = run_readonly(path, recipes_v1.status_session)
            elif args.cmd == "verify":
                res = _verify(path, args.against)
            elif args.cmd == "sync":
                if args.live:
                    make_backup(path, "recipes_sync")
                res = _tx(path, recipes_v1.sync_session, name="recipes_sync")
            elif args.cmd == "strip":
                make_backup(path, "recipes_strip")
                res = _tx(path, recipes_v1.strip_session, name="strip")
            else:  # drop
                make_backup(path, "recipes_drop")
                res = _tx(path, recipes_v1.drop_session, name="drop")
    except Refused as exc:
        print(f"отказ: {exc}", file=sys.stderr)
        return 2
    except SyncFailed as exc:
        logger.error("миграция: %s", exc)
        print(f"ошибка: {exc}", file=sys.stderr)
        return 3 if args.cmd == "apply" else 1
    except MigrationError as exc:
        logger.error("миграция: %s", exc)
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    _print(args.cmd, res, args.json)
    if args.cmd == "verify" and not res.get("ok"):
        return 1
    return 0


def _verify(path: Path, against: str | None) -> dict:
    originals = kept = None
    if against:
        originals, kept = run_readonly(Path(against).expanduser().resolve(), lambda s: (
            {r.id: r.dishes for r in recipes_v1.load_rows(s)}, recipes_v1.kept_rows(s)))

    def check(s: Session):
        if recipes_v1.marker(s) is None:
            raise MigrationError("recipes_v1 не применён — сверять нечего")
        return recipes_v1.verify_session(s, originals=originals, strict_refs=False,
                                         kept_before=kept)

    return run_readonly(path, check)


if __name__ == "__main__":
    sys.exit(main())
