#!/usr/bin/env bash
# Easy Week — обновление на Raspberry Pi (нативно, без Docker).
# Тянет свежий код, пересобирает бэк и фронт, перезапускает сервис и nginx.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

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
echo "   Через интернет/HTTPS — по адресу Cloudflare Tunnel (см. deploy/README.md)."
