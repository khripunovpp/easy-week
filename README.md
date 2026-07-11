# Easy Week

Домашний чат-планировщик рецептов на неделю. Подбирает блюда, считает время готовки,
даёт инструкции по приготовлению и хранению (вакуум + заморозка), собирает список покупок.
Экспорт в PDF (по частям или целиком). Планы имеют статусы (черновик/принят/отклонён),
принятые недели учитываются при новой генерации.

- **Frontend:** Angular (PWA)
- **Backend:** FastAPI + SQLite (SQLModel)
- **AI:** Cloudflare Workers AI (REST, structured JSON output; генерация блюд параллельно, шаги — лениво)
- **Хостинг:** Raspberry Pi (systemd + nginx) + Cloudflare Tunnel

Подробный план — в [`PLAN.md`](./PLAN.md). Деплой — в [`deploy/README.md`](./deploy/README.md).

## Структура

```
easy-week/
  frontend/   # Angular PWA (чат + рендер страниц плана)
  backend/    # FastAPI + SQLite
  deploy/     # деплой на Raspberry Pi (systemd, nginx, cloudflared)
```

## Запуск (dev)

Нужны два процесса. Фронт ходит на относительный `/api`, dev-прокси (`frontend/proxy.conf.json`)
перенаправляет его на бэкенд :8000.

```bash
# 1) Backend  → http://localhost:8000
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # впиши CF_ACCOUNT_ID / CF_API_TOKEN (Cloudflare Workers AI)
uvicorn app.main:app --reload

# 2) Frontend → http://localhost:4200
cd frontend && npm install && npm start
```

## Деплой

На Raspberry Pi — нативно (systemd + nginx + Cloudflare Tunnel), одной командой обновление:

```bash
cd ~/easy-week && bash deploy/update.sh
```

Полная пошаговая инструкция (первая настройка, порты, HTTPS-туннель) — в
[`deploy/README.md`](./deploy/README.md). Есть и `docker-compose.yml` как опция.
