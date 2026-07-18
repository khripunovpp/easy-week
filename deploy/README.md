# Деплой Easy Week на Raspberry Pi (нативно, через systemd)

Тот же подход, что у perdulário: без Docker. FastAPI-бэкенд как systemd-сервис +
nginx раздаёт фронт и проксирует `/api`. Плюс Cloudflare Tunnel для HTTPS
(нужен, чтобы PWA устанавливалась и работала офлайн — по LAN-http service worker не активируется).

Данные Пая (как у perdulário):
- пользователь: **pashtitto**, IP: **192.168.1.230**
- каталог проекта: **/home/pashtitto/easy-week**
- **порты:** бэкенд **8010**, nginx Easy Week **8080** (perdulário уже держит :80, 3001, 5432; «другой проект» — 3000)

---

## 1. Один раз: зависимости на Пае

Node и nginx уже стоят (для perdulário). Нужен Python 3:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
# cloudflared (если ещё нет)
# см. https://pkg.cloudflare.com — для arm64:
# curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /usr/local/bin/cloudflared && sudo chmod +x /usr/local/bin/cloudflared
```

## 2. Забрать код и настроить .env

```bash
git clone <URL-репозитория> /home/pashtitto/easy-week
cd /home/pashtitto/easy-week/backend
cp .env.example .env
# Впиши Cloudflare Workers AI креды (те же, что в perdulário/backend/.env):
#   CF_ACCOUNT_ID=...
#   CF_API_TOKEN=...
nano .env
```

## 3. Собрать бэк и фронт (первый раз)

```bash
cd /home/pashtitto/easy-week/backend
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt

cd /home/pashtitto/easy-week/frontend
npm ci
npm run build          # → dist/frontend/browser
```

## 4. Сервис бэкенда (systemd)

```bash
sudo cp /home/pashtitto/easy-week/deploy/easy-week-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now easy-week-backend
systemctl status easy-week-backend
curl -s http://127.0.0.1:8010/api/health   # {"status":"ok",...}
```

## 5. nginx (раздача фронта + прокси /api) на :8080

```bash
sudo cp /home/pashtitto/easy-week/deploy/nginx-easy-week.conf /etc/nginx/sites-available/easy-week
sudo ln -sf /etc/nginx/sites-available/easy-week /etc/nginx/sites-enabled/easy-week
sudo nginx -t && sudo systemctl reload nginx
```

В домашней сети уже доступно: **http://192.168.1.230:8080**
(но PWA-установка/офлайн заработают только по HTTPS — шаг 6).

## 6. HTTPS — Tailscale Funnel (рекомендуется, без домена)

Самый простой способ дать приложению HTTPS (нужен, чтобы **service worker и офлайн PWA
заработали** — по LAN-http SW не регистрируется) и доступ извне (из магазина и т.п.) без покупки
домена. Даёт стабильный адрес вида `https://<имя-пая>.<твой-tailnet>.ts.net`.

```bash
# 1) Установить Tailscale на Пае
curl -fsSL https://tailscale.com/install.sh | sh

# 2) Залогиниться (откроет URL — подтвердить в браузере под своим аккаунтом Tailscale)
sudo tailscale up

# 3) В админке Tailscale (login.tailscale.com/admin):
#    - DNS → включить MagicDNS и «HTTPS Certificates»;
#    - Access controls / node → разрешить Funnel для этого узла (атрибут funnel).

# 4) Опубликовать nginx Easy Week (:8080) наружу по HTTPS, персистентно:
sudo tailscale funnel --bg 8080

# 5) Посмотреть выданный адрес:
tailscale funnel status      # покажет https://<pi>.<tailnet>.ts.net
```

Дальше **устанавливать PWA нужно именно с этого `https://…ts.net`** (SW и кэш привязаны к origin;
установка со старого `http://192.168.1.230:8080` офлайн работать не будет). Манифест и nginx уже
готовы: `manifest.webmanifest` использует относительные `scope`/`start_url`, а nginx слушает
`server_name _` — принимает любой Host. Локальный `http://192.168.1.230:8080` остаётся для доступа
в домашней сети без интернета.

## 6-alt. Cloudflare Tunnel (если нужен свой домен)

```bash
cloudflared tunnel login
cloudflared tunnel create easy-week
# конфиг:
cp /home/pashtitto/easy-week/deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
nano ~/.cloudflared/config.yml          # подставь TUNNEL_ID и hostname
# если есть домен в Cloudflare:
cloudflared tunnel route dns easy-week easy-week.ТВОЙ-ДОМЕН
# как сервис (автозапуск):
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Теперь приложение по HTTPS → можно «Добавить на экран», SW и офлайн работают 🥕

**Быстрая альтернатива без домена** (эфемерный URL для проверки):
```bash
cloudflared tunnel --url http://localhost:8080
# выдаст https://<random>.trycloudflare.com
```

---

## Обновление после изменений

```bash
cd /home/pashtitto/easy-week
bash deploy/update.sh
```
(git pull → пересборка бэка и фронта → рестарт сервиса + reload nginx)

## Полезное

```bash
journalctl -u easy-week-backend -f          # логи бэкенда
sudo systemctl restart easy-week-backend    # перезапуск API
cp backend/data/easy_week.db backup_$(date +%F).db   # бэкап БД (SQLite — просто файл)
```

---

## Альтернатива: Docker

Все docker-конфиги — в `docker/` (compose + Dockerfile'ы). Весь стек одной командой из этой папки
(фронт на `${APP_PORT:-8080}`):
```bash
cp backend/.env.example backend/.env    # впиши CF/DeepSeek/Gemini-креды
cd docker
docker compose up -d --build                       # backend + frontend
docker compose --profile monitoring up -d --build  # + Prometheus/Loki/Promtail/Grafana
```
Cloudflare Tunnel в этом случае указывай на тот же `http://localhost:8080`.
Основной путь для этого Пая — нативный (выше). Подробнее — `docker/README.md`.
