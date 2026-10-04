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

### Пароль на вход (обязательно для доступа извне)

Приложение закрыто одним общим паролем (на всех устройствах один). При первом заходе
устройство спрашивает пароль, дальше помнит его ~год (HttpOnly-кука `ew_session`).
Пароль **не коммитим** — только в `backend/.env` на Пае:

```bash
# backend/.env
APP_PASSWORD=придумай-пароль
# необязательно: отдельный ключ подписи сессий (иначе выводится из пароля).
# Сгенерировать: python3 -c 'import secrets; print(secrets.token_hex(32))'
APP_SECRET=...
# необязательно: флаг Secure у куки — auto (по https/X-Forwarded-Proto, дефолт) | true | false
# AUTH_COOKIE_SECURE=auto
```

- Пустой `APP_PASSWORD` → вход **выключен** (удобно в деве), в логе при старте warning.
- Смена `APP_PASSWORD` (или `APP_SECRET`) → все устройства разлогинятся и спросят пароль заново.
- После правки `.env`: `sudo systemctl restart easy-week-backend`.
- Закрыты все `/api/*`, кроме `/api/auth/*` и `/api/health`. Неверный пароль — не больше
  5 попыток в минуту с одного IP (IP берётся из `X-Forwarded-For`, его ставит nginx).
- `/metrics` паролем не закрыт: Prometheus скрапит `127.0.0.1:8010` напрямую, а в nginx
  `/metrics` открыт только для localhost / `192.168.0.0/16` / tailnet `100.64.0.0/10`
  (запросы через Funnel/туннель — 403).

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

Реальная настройка (Пай `pashtitto`, адрес получился `https://pashtitto.tail36c191.ts.net`):

```bash
# 1) Установить Tailscale на Пае
curl -fsSL https://tailscale.com/install.sh | sudo sh

# 2) Залогиниться (печатает URL — подтвердить в браузере под своим аккаунтом Tailscale)
sudo tailscale up

# 3) ⚠️ DNS-ЛОВУШКА. Роутер этого Пая отдаёт DNS-серверы в диапазоне 100.64.0.0/10
#    (100.90.1.1 и т.п.) — ровно его Tailscale забирает под свой оверлей, и при поднятом
#    Tailscale интернет на Пае отваливается (curl/git/модели виснут). Поэтому даём Паю
#    ПУБЛИЧНЫЙ DNS (вне этого диапазона) и просим Tailscale НЕ трогать DNS:
sudo nmcli con mod "DIGIFIBRA-PLUS-E51A" ipv4.dns "1.1.1.1 8.8.8.8" ipv4.ignore-auto-dns yes
sudo nmcli con up  "DIGIFIBRA-PLUS-E51A"        # переактивация Wi-Fi (тот же IP)
sudo tailscale set --accept-dns=false
#    Проверка: getent hosts github.com  — должно резолвить при поднятом Tailscale.

# 4) В админке Tailscale (login.tailscale.com/admin): DNS → «HTTPS Certificates» = Enabled.
#    Funnel включается один раз по ссылке, которую печатает шаг 5 (login.tailscale.com/f/funnel?...).

# 5) Опубликовать nginx Easy Week (:8080) наружу по HTTPS, персистентно:
sudo tailscale funnel --bg 8080     # выпустит cert, покажет https://<pi>.<tailnet>.ts.net
sudo tailscale funnel status        # проверить
```

Дальше **устанавливать PWA нужно именно с `https://…ts.net`** (SW и кэш привязаны к origin;
установка со старого `http://192.168.1.230:8080` офлайн работать не будет). Манифест и nginx уже
готовы: `manifest.webmanifest` использует относительные `scope`/`start_url`, а nginx слушает
`server_name _` — принимает любой Host. Локальный `http://192.168.1.230:8080` остаётся для доступа
в домашней сети без интернета. Всё персистентно: `tailscaled` — systemd-сервис (автозапуск),
funnel-конфиг и DNS-правка сохраняются между ребутами.

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
(бэкап → git pull → пересборка бэка и фронта → рестарт сервиса + reload nginx)

**Бэкап обязателен:** если `deploy/backup.sh` упал, `update.sh` останавливается ДО `git pull` —
код, база и сервис не тронуты. Починить причину (место на карте, python3) и повторить; осознанно
катить без бэкапа — `EW_SKIP_BACKUP=1 bash deploy/update.sh`.

⚠️ `update.sh` не копирует конфиг nginx. Если менялся `deploy/nginx-easy-week.conf` —
повтори шаг 5 (`sudo cp … && sudo nginx -t && sudo systemctl reload nginx`).

## Полезное

```bash
journalctl -u easy-week-backend -f          # логи бэкенда
sudo systemctl restart easy-week-backend    # перезапуск API
bash deploy/backup.sh                       # бэкап вручную (БД + JSON-состояние)
```

### Бэкапы

`deploy/backup.sh` делает консистентную копию SQLite (online-backup API, не `cp` живой базы)
+ `preferences.json`/`app_state.json`/`usage-limits.json`/`settings.json` в `~/easy-week-backups/`
(каталог — `EW_BACKUP_DIR`). Запускается:
- ночью из cron: `crontab -e` → `15 4 * * * bash ~/easy-week/deploy/backup.sh >> ~/easy-week-backups/backup.log 2>&1`
  → `easy-week-<дата>.tar.gz`, хранит 14 последних (`EW_BACKUP_KEEP`);
- автоматически в начале `deploy/update.sh` → `easy-week-predeploy-<дата>.tar.gz`, хранит 10
  последних (`EW_BACKUP_KEEP_PREDEPLOY`).

Ротация у ночных и предеплойных архивов раздельная: день с десятком деплоев не вытесняет ночные
точки отката (окно — две недели). Архивы с другим именем (ручные, напр. `pre-recipes-*`)
ротация не трогает.

Восстановление: остановить сервис, распаковать архив в `backend/data/`, запустить
(подробно — «Откат и восстановление» ниже).

---

## Откат и восстановление (runbook)

Все бэкапы `backup.sh` лежат на той же SD-карте, что и база: карта умрёт — уйдут все точки
отката. Поэтому перед рискованными шагами (перенос рецептов в таблицы — фаза 1, «похудение»
планов — фаза 3) делаем копию **вне Пая** и **учебное восстановление**.

### Копия вне Пая (с ноутбука, через Tailscale)

```bash
# на ноутбуке; pi5 — ssh-алиас Пая (пользователь pashtitto)
mkdir -p ~/easy-week-offsite
rsync -av pi5:easy-week-backups/ ~/easy-week-offsite/     # все архивы (ночные + перед деплоем)
ls -lt ~/easy-week-offsite | head -3                        # свежий архив на месте
```

Нужен архив посвежее — сначала `ssh pi5 'bash ~/easy-week/deploy/backup.sh'`.

### Учебное восстановление (restore drill) — на Пае, рабочую базу не трогает

```bash
LATEST="$(ls -1t ~/easy-week-backups/easy-week-*.tar.gz | head -1)"
DRILL="$(mktemp -d ~/ew-drill-XXXX)"
tar -xzf "$LATEST" -C "$DRILL"
# 1) база цела и читается (read-only)
python3 - "$DRILL/easy_week.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(c.execute("PRAGMA integrity_check").fetchone()[0])            # ok
print(c.execute("SELECT count(*) FROM planrow").fetchone()[0], "планов")
PY
# 2) приложение поднимается на копии: свой порт, cwd без .env → нет ключей моделей
#    (никаких AI-вызовов) и нет пароля; все data-файлы — из $DRILL (DB_PATH)
cd "$DRILL" && DB_PATH="$DRILL/easy_week.db" \
  ~/easy-week/backend/.venv/bin/uvicorn --app-dir ~/easy-week/backend app.main:app \
  --host 127.0.0.1 --port 8099 &
sleep 5
curl -s 127.0.0.1:8099/api/health
curl -s 127.0.0.1:8099/api/plans | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "планов в списке")'
curl -s 127.0.0.1:8099/api/recipes | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "рецептов")'
PID="$(curl -s 127.0.0.1:8099/api/plans | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
curl -s "127.0.0.1:8099/api/plans/$PID" | head -c 300; echo
# 3) убрать за собой
kill %1; cd ~ && rm -rf "$DRILL"
```

Числа должны совпасть с рабочим приложением (`/api/plans`, `/api/recipes`). Покупки, план
готовки и PDF на копии не открывать — они догенерируют рецепты (ключей нет — просто ошибка).

### Уровни отката

- **L0 — автоматически.** Упал бэкап — `update.sh` остановился до `git pull`, ничего не
  поменялось. Сбой модели ничего не пишет (вариант рецепта — только после успеха). Запись блюд
  плана идёт через `services/planstore` с проверкой версии (`planrow.dishes_version`): если план
  трижды подряд переписали параллельно — 409, записанное раньше цело.
- **L1 — только чтение рецептов из JSON** (`EW_RECIPE_STORE=json` в `backend/.env` + рестарт) —
  появится с фазы 2 (чтение из таблиц рецептов). Сейчас всё читается из JSON планов — нечего
  переключать.
- **L2 — откат кода** (фазы 0a/0b и дальше до 2b): `git revert <коммиты фазы>` → `git push` →
  `bash deploy/update.sh`. Старый код игнорирует новые ключи вариантов (`model_ref`, `kind`,
  `change`, `parent_id`, `ctx_uses`, `gen_id`) и колонку `dishes_version` (nullable, остаётся —
  безвредна). Без `git reset` в скриптах: откат — обычный коммит.
- **L3 — снять миграцию рецептов** (`python -m app.migrations strip/drop --live`) — с фазы 1;
  до неё переносить нечего.
- **L4 — катастрофа** (файл базы битый). Всё, записанное после бэкапа, теряется.
  Битую базу откладываем ВМЕСТЕ с её журналом (`easy_week.db-journal`, при WAL — `-wal`/`-shm`):
  SQLite применяет оставшийся рядом журнал к любому файлу с именем `easy_week.db` — журнал битой
  базы «откатил» бы свои страницы прямо в восстановленную копию и молча её испортил.

  ```bash
  sudo systemctl stop easy-week-backend
  cd ~/easy-week
  B=~/ew-broken-$(date +%F) && mkdir -p "$B" && mv backend/data/easy_week.db* "$B"/
  ls backend/data/easy_week.db* 2>/dev/null                 # пусто — рядом ни базы, ни журнала
  tar -xzf "$(ls -1t ~/easy-week-backups/easy-week-*.tar.gz | head -1)" -C backend/data/
  # только чтение: чужой журнал рядом не применится, а всплывёт ошибкой
  python3 -c 'import sqlite3, sys; c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); print(c.execute("PRAGMA integrity_check").fetchone()[0])' backend/data/easy_week.db   # ok
  sudo systemctl start easy-week-backend
  ```

  Архив — из `~/easy-week-backups` (свежий любого вида) или из копии вне Пая. Пока проверка не
  напечатала `ok`, сервис не запускать. «attempt to write a readonly database» — рядом с файлом
  всё ещё чужой журнал: убрать его (`mv backend/data/easy_week.db* "$B"/`) и распаковать архив
  заново; иная ошибка — битый сам архив, берём предыдущий (`ls -1t ~/easy-week-backups`).

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
