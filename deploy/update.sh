#!/usr/bin/env bash
# Easy Week — обновление на Raspberry Pi (нативно, без Docker).
# Тянет свежий код, пересобирает бэк и фронт, перезапускает сервис и nginx.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Бэкап перед обновлением — на случай неудачной миграции/деплоя. Без свежего бэкапа НЕ
# катим: это точка отката (deploy/README.md → «Откат»). Осознанно без бэкапа (кончилось место,
# сломан python3 и т.п.) — EW_SKIP_BACKUP=1 bash deploy/update.sh.
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

echo "→ git pull"
git pull --ff-only

echo "→ backend: venv + зависимости"
cd "$REPO_DIR/backend"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "→ frontend: install + build"
cd "$REPO_DIR/frontend"
npm ci
npm run build

echo "→ restart backend service + reload nginx"
sudo systemctl restart easy-week-backend
sudo nginx -t && sudo systemctl reload nginx

echo "✅ Готово. Локально: http://$(hostname -I | awk '{print $1}'):8080/"
# Внешний HTTPS-адрес — из Tailscale Funnel (deploy/README.md, шаг 6); нет funnel — подсказка.
FUNNEL_URL="$(tailscale funnel status 2>/dev/null | grep -o 'https://[^ ]*' | head -1 || true)"
if [ -n "$FUNNEL_URL" ]; then
  echo "   Вне сети (HTTPS, PWA): $FUNNEL_URL"
else
  echo "   ⚠️ Tailscale Funnel не включён — вне сети не откроется (см. deploy/README.md, шаг 6)."
fi
