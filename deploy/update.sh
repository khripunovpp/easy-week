#!/usr/bin/env bash
# Easy Week — обновление на Raspberry Pi (нативно, без Docker).
# Бэкап → свежий код → бэк (+ репетиция миграций на копии базы) → фронт → стоп сервиса →
# миграция живой базы → старт сервиса → nginx.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
SELF="$REPO_DIR/deploy/update.sh"

if [ "${EW_UPDATE_REEXEC:-0}" != "1" ]; then
  # Бэкап перед обновлением — на случай неудачной миграции/деплоя. Без свежего бэкапа НЕ
  # катим: это точка отката (deploy/README.md → «Откат»). Осознанно без бэкапа (кончилось
  # место, сломан python3 и т.п.) — EW_SKIP_BACKUP=1 bash deploy/update.sh.
  echo "→ backup"
  if ! EW_BACKUP_TAG=predeploy bash "$REPO_DIR/deploy/backup.sh"; then
    if [ "${EW_SKIP_BACKUP:-0}" = "1" ]; then
      echo "⚠️ бэкап не удался — EW_SKIP_BACKUP=1, продолжаем без бэкапа"
    else
      echo "❌ бэкап не удался — деплой остановлен (код и сервис не тронуты)." >&2
      echo "   Починить бэкап или осознанно: EW_SKIP_BACKUP=1 bash deploy/update.sh" >&2
      exit 1
    fi
  fi

  SELF_SUM="$(sha1sum "$SELF" | cut -d' ' -f1)"
  echo "→ git pull"
  git pull --ff-only
  # bash дочитывает УЖЕ открытый файл (git заменил update.sh новым файлом): иначе этот деплой
  # прошёл бы по старым шагам — без новой миграции, а откат кода — с шагами, которых в коде
  # уже нет. Изменился — перезапуск новой версией, без повторного бэкапа и pull.
  if [ "$(sha1sum "$SELF" | cut -d' ' -f1)" != "$SELF_SUM" ]; then
    echo "→ update.sh изменился в git pull — перезапуск новой версией"
    exec env EW_UPDATE_REEXEC=1 bash "$SELF" "$@"
  fi
else
  echo "→ (перезапуск после git pull: бэкап и pull уже сделаны)"
fi

echo "→ backend: venv + зависимости"
cd "$REPO_DIR/backend"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# Миграции — только если они есть в этом коде. Проверяем файл __main__.py, а не каталог: после
# отката кода в app/migrations остаётся __pycache__ (в .gitignore), и каталог — пустой
# namespace-пакет, который `python -m` запустить не может.
HAS_MIGRATIONS=0
if [ -f "$REPO_DIR/backend/app/migrations/__main__.py" ]; then HAS_MIGRATIONS=1; fi

# Миграции хранилища (backend/app/migrations, deploy/README.md → «Миграции»). Сначала —
# РЕПЕТИЦИЯ на копии базы (живая открыта только на чтение): apply + сверка, повторный apply,
# drop + apply. Не прошла — стоп ДО сборки и рестарта: живая база не тронута, сервис работает
# на старом коде из памяти, а новый код на диске без маркера своей миграции ведёт себя как
# прежний (если Пай перезагрузится). Чиним обычным коммитом или revert — не git reset.
if [ "$HAS_MIGRATIONS" = "1" ]; then
  echo "→ миграции: репетиция на копии базы"
  if ! ./.venv/bin/python -m app.migrations rehearse; then
    echo "❌ репетиция миграции не прошла — деплой остановлен до сборки и рестарта" >&2
    echo "   (живая база не тронута). Копия и отчёт — backend/data/backups/." >&2
    exit 1
  fi
else
  echo "→ миграций в этом коде нет (app/migrations/__main__.py) — без репетиции"
fi

echo "→ frontend: install + build"
cd "$REPO_DIR/frontend"
npm ci
npm run build

# Рестарт = стоп → миграция живой базы → старт. Миграция пишет только при остановленном
# сервисе (apply --live сам это проверяет), делает свою копию базы и коммитит одной транзакцией
# после сверки. Старт — ВСЕГДА (trap): без маркера миграции приложение работает на JSON, так что
# сбой миграции громкий (код выхода), но сервис не лежит.
cd "$REPO_DIR/backend"
MIGRATE_RC=0
if [ "$HAS_MIGRATIONS" = "1" ]; then
  echo "→ stop backend → миграция (apply --live) → start backend"
  sudo systemctl stop easy-week-backend
  trap 'sudo systemctl start easy-week-backend' EXIT
  ./.venv/bin/python -m app.migrations apply --live || MIGRATE_RC=$?
  sudo systemctl start easy-week-backend
  trap - EXIT
else
  echo "→ restart backend"
  sudo systemctl restart easy-week-backend
fi
echo "→ reload nginx"
sudo nginx -t && sudo systemctl reload nginx

if [ "$MIGRATE_RC" -eq 0 ]; then
  echo "✅ Готово. Локально: http://$(hostname -I | awk '{print $1}'):8080/"
else
  echo "⚠️ Код обновлён и сервис запущен, но миграция не прошла (см. ниже)." \
       "Локально: http://$(hostname -I | awk '{print $1}'):8080/"
fi
# Внешний HTTPS-адрес — из Tailscale Funnel (deploy/README.md, шаг 6); нет funnel — подсказка.
FUNNEL_URL="$(tailscale funnel status 2>/dev/null | grep -o 'https://[^ ]*' | head -1 || true)"
if [ -n "$FUNNEL_URL" ]; then
  echo "   Вне сети (HTTPS, PWA): $FUNNEL_URL"
else
  echo "   ⚠️ Tailscale Funnel не включён — вне сети не откроется (см. deploy/README.md, шаг 6)."
fi

if [ "$MIGRATE_RC" -ne 0 ]; then
  # Что именно с базой — по коду выхода CLI (app/migrations/__main__.py), а не «всегда прежняя»:
  # sync — своя транзакция ПОСЛЕ закоммиченного шага.
  case "$MIGRATE_RC" in
    2) echo "❌ миграция отказалась (код 2): база не тронута (сервис или другой процесс держит" \
            "базу? см. «отказ:» выше)." >&2 ;;
    3) echo "❌ шаг миграции применён (маркер есть — двойная запись включена, читается JSON)," \
            "но sync не прошёл (код 3): его транзакция откачена." >&2 ;;
    *) echo "❌ миграция/sync не прошли (код $MIGRATE_RC): их транзакция откачена — данные" \
            "как до этого деплоя; сервис запущен." >&2 ;;
  esac
  echo "   Состояние базы сейчас (только чтение):" >&2
  ./.venv/bin/python -m app.migrations status --live >&2 || true
  echo "   Подробности — вывод выше и backend/data/backups/*-verify-*.json." >&2
  exit "$MIGRATE_RC"
fi
