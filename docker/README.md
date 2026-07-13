# Docker — весь стек из одного места

Все docker-конфиги (compose + Dockerfile'ы) лежат здесь. Запускать из этой папки: `cd docker`.

| Что | Команда |
|-----|---------|
| Бэкенд + фронт | `docker compose up -d --build` |
| + мониторинг (Prometheus/Loki/Promtail/Grafana) | `docker compose --profile monitoring up -d --build` |
| Остановить | `docker compose down` (добавь `--profile monitoring`, если поднимал его) |
| Логи | `docker compose logs -f backend` |

- Перед первым запуском заполни `../backend/.env` (шаблон — `../backend/.env.example`),
  включая `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, CF-креды.
- Приложение: <http://localhost:8080> (порт меняется `APP_PORT`).
- Grafana: <http://localhost:3000> (admin / admin).
- Dev auto: `docker-compose.override.yml` подхватывается сам — бэкенд с `--reload` и
  примонтированным кодом (правки без пересборки).

## Что где
- `docker-compose.yml` — единый стек: `backend`, `frontend` + мониторинг под профилем `monitoring`
  (один проект `easy-week`, общая сеть и том `easy-week_ewdata` с AI-логами).
- `docker-compose.override.yml` — dev-оверрайд (live-reload бэкенда).
- `backend.Dockerfile`, `frontend.Dockerfile` — образы (build-контекст остаётся `../backend` / `../frontend`).
- Конфиги самих сервисов мониторинга (prometheus/loki/promtail/grafana `*.docker.yml`,
  дашборды) живут в `../monitoring/` рядом с нативным Пай-деплоем и монтируются отсюда.

На Raspberry Pi деплой **нативный** (systemd), не через этот compose — см. `../deploy/README.md`
и `../monitoring/install-pi.sh`.
