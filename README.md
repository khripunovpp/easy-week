# Easy Week

Домашний чат-планировщик рецептов на неделю. Подбирает блюда, считает время готовки,
даёт инструкции по приготовлению и хранению (вакуум + заморозка), собирает список покупок.
Экспорт в PDF (по частям или целиком) и в JSON (для внешнего расчёта себестоимости).

- **Frontend:** Angular (PWA)
- **Backend:** FastAPI + SQLite (SQLModel)
- **AI:** Cloudflare Workers AI (REST, structured JSON output)
- **Хостинг:** Raspberry Pi + Cloudflare Tunnel

Подробный план — в [`PLAN.md`](./PLAN.md).

## Структура

```
easy-week/
  frontend/   # Angular PWA (чат + рендер страниц плана)
  backend/    # FastAPI + SQLite
  deploy/     # деплой на Raspberry Pi
```

## Запуск (dev)

```bash
# Frontend
cd frontend && npm install && npm start   # http://localhost:4200

# Backend (позже)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload             # http://localhost:8000
```
