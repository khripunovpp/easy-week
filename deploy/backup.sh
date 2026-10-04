#!/usr/bin/env bash
# Easy Week — бэкап состояния: ночной (cron на Пае) и перед каждым деплоем (update.sh),
# см. deploy/README.md. SQLite копируется через online-backup API (безопасно на живой базе,
# в отличие от cp), JSON-файлы состояния — tar'ом.
#
# Ротация у каждого вида своя: деплой идёт после каждого коммита, и при общей ротации день
# с десятком деплоев вытеснял все ночные архивы — откатиться на позавчера было бы не к чему.
#   ночной        easy-week-<дата>.tar.gz            — последние EW_BACKUP_KEEP (14)
#   перед деплоем easy-week-predeploy-<дата>.tar.gz  — последние EW_BACKUP_KEEP_PREDEPLOY (10)
# Вид задаёт EW_BACKUP_TAG=predeploy (update.sh); без него — ночной.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/backend/data"
BACKUP_DIR="${EW_BACKUP_DIR:-$HOME/easy-week-backups}"
TAG="${EW_BACKUP_TAG:-}"
if [ -n "$TAG" ]; then
  KEEP="${EW_BACKUP_KEEP_PREDEPLOY:-10}"
else
  KEEP="${EW_BACKUP_KEEP:-14}"
fi
PREFIX="easy-week-${TAG:+$TAG-}"
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
for f in preferences.json app_state.json usage-limits.json settings.json prices.json; do
  [ -f "$DATA_DIR/$f" ] && cp "$DATA_DIR/$f" "$TMP/"
done

OUT="$BACKUP_DIR/$PREFIX$STAMP.tar.gz"
tar -czf "$OUT" -C "$TMP" .

# Ротация: KEEP самых свежих архивов СВОЕГО вида. После префикса сразу цифра даты — ночная
# маска не цепляет predeploy-архивы, а ручные (pre-recipes-* и т.п.) не трогает никто.
ls -1t "$BACKUP_DIR/$PREFIX"[0-9]*.tar.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "✅ backup: $OUT"
