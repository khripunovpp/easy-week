# Easy Week — мониторинг (Prometheus + Grafana + Loki)

Наблюдаемость: **метрики** (Prometheus) + **логи** (Loki) + **дашборды** (Grafana).
Метрики бэкенда — на `/metrics` (кол-во/задержка/статусы запросов + свои счётчики токенов и
кэша: `easyweek_ai_calls_total`, `easyweek_ai_tokens_total{kind=prompt|completion|cache_hit|cache_miss}`).
Логи — journald бэкенда + структурный AI-лог `backend/data/ai-logs/*.jsonl`.

Что где смотреть:
- **Grafana** — дашборд «Easy Week — AI и запросы» (токены, % кэша DeepSeek, вызовы по категориям,
  HTTP-статусы, латентность p95, логи).
- **Prometheus** — сырые метрики/запросы PromQL.
- **Loki** — поиск по логам (через Grafana Explore).

---

## Локально (Docker)

Приложение и мониторинг — один compose-проект в `docker/`. Мониторинг — профиль `monitoring`:
```bash
cd docker
docker compose --profile monitoring up -d --build  # backend + frontend + prometheus/loki/promtail/grafana
```
Grafana: http://localhost:3000 (admin / admin). Остановить: `docker compose --profile monitoring down`.
Конфиги сервисов мониторинга (`*.docker.yml`, дашборды) лежат здесь, в `monitoring/`, и монтируются
из `docker/docker-compose.yml`. Подробнее про запуск — `docker/README.md`.

## На Raspberry Pi (native, без Docker)

Бинарники Prometheus/Loki/Promtail + systemd, Grafana из apt. Один раз:
```bash
cd ~/easy-week && git pull
bash monitoring/install-pi.sh
```
- **Grafana**: `http://192.168.1.230:3002` (admin/admin — сменить при входе).
- **Prometheus** слушает только localhost (9090); метрики в LAN — `http://192.168.1.230:8080/metrics`.

Конфиги живут в репо (`monitoring/`), systemd на них ссылается — правки применяются
`git pull` + `sudo systemctl restart ew-prometheus ew-loki ew-promtail`.

Порты (localhost на Пае): Prometheus 9090, Loki 3100, Promtail 9080; Grafana 3002 (LAN).

## Файлы
- `prometheus/prometheus.{pi,docker}.yml` — скрап-конфиги (таргет бэкенда).
- `loki/loki-config.yml` — Loki single-binary, хранение на диске, ретеншн 30 дней.
- `promtail/promtail.{pi,docker}.yml` — сбор логов в Loki.
- `grafana/` — datasources, провайдер дашбордов, сам дашборд `dashboards/easy-week.json`.
- `systemd/ew-*.service` — юниты для Пая. `install-pi.sh` — установка.
