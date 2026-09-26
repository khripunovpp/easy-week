#!/usr/bin/env bash
# Easy Week — ночной бэкап состояния (запускается из cron на Пае, см. deploy/README.md).
# SQLite копируется через online-backup API (безопасно на живой базе, в отличие от cp),
# JSON-файлы состояния — tar'ом. Храним последние KEEP копий.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/backend/data"
BACKUP_DIR="${EW_BACKUP_DIR:-$HOME/easy-week-backups}"
KEEP="${EW_BACKUP_KEEP:-14}"
STAMP="$(date +%Y-%m-%d-%H%M)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$BACKUP_DIR"

# Консистентная копия базы: sqlite3.Connection.backup держит снапшот, пока копирует.
python3 - "$DATA_DIR/easy_week.db" "$TMP/easy_week.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY

# JSON-состояние (предпочтения, текущий план, лимиты, настройки моделей) — рядом с базой.
for f in preferences.json app_state.json usage-limits.json settings.json; do
  [ -f "$DATA_DIR/$f" ] && cp "$DATA_DIR/$f" "$TMP/"
done

tar -czf "$BACKUP_DIR/easy-week-$STAMP.tar.gz" -C "$TMP" .

# Ротация: оставляем KEEP самых свежих архивов.
ls -1t "$BACKUP_DIR"/easy-week-*.tar.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "✅ backup: $BACKUP_DIR/easy-week-$STAMP.tar.gz"
