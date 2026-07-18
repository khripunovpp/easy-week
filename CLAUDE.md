# Easy Week — правила проекта

## Роутинг AI-моделей (главное правило)

**Рецепты придумывает модель, выбранная пользователем.** Выбор — в профиле (глобальный дефолт)
и в чате (override только для этого чата, профиль не трогает). Модели: **DeepSeek**
(`deepseek-chat`), **Gemini** (`gemini-flash-latest`), **Claude** (`claude-opus-4-8`, Anthropic API —
нужен баланс кредитов в console.anthropic.com), **Cloudflare** (mistral-пайплайн).
Сюда входит генерация плана (блюда + короткие шаги) и развёрнутые пошаговые рецепты —
всё той же выбранной моделью (деталь = текущая модель чата, не пиннится к модели плана).

**Фолбэков между моделями НЕТ.** Выбранная модель либо отвечает, либо кидает `AIError` →
роутер отдаёт 502 / `event: error`, и фронт просит переключить модель или собрать план заново.

**Вспомогательные задачи — всегда Cloudflare Workers AI** (mistral,
`@cf/mistralai/mistral-small-3.1-24b-instruct`): нормализация списка покупок. Не участвуют
в пользовательском выборе. (База списка покупок собирается детерминированно из ингредиентов.)

Провайдеры логируются с меткой: `AI → DeepSeek · …` / `AI → Gemini · …` / `AI → Cloudflare · …`.

### Архитектура (гейты)
Наглядная схема (потоки + классы + матрица задач) — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Каждый провайдер — подкласс `ModelGate` (`ai/base.py`): `complete_json` (шаблонный метод с
ретраями/логом), хуки `_request_json`/`stream_json`/`call_tools`. Реестр и выбор — `ai/gates.py`
(`gate_for(model_key)`). `ai/planner.py` роутит по выбранной модели, без `try/except → другой провайдер`.
- DeepSeek/Gemini/Claude — план одним запросом; Cloudflare — пайплайн меню→спеки→валидатор.
- Правки: DeepSeek — function calling; Gemini/Claude/Cloudflare — structured actions.
- Gemini: `thinkingBudget=0` (рецептам reasoning не нужен, иначе обрывает JSON).
- Claude: system — отдельным полем; температуру не шлём (Opus 4.8/4.7 её отвергают); JSON-режима
  нет — просим строгий JSON в промпте и лениво парсим (снимаем ```-ограждение).
- Новый ключ Google не даёт `gemini-2.5-flash` — используем алиас `gemini-flash-latest`.

## Дизайн (обязательно)

**Перед любой задачей по вёрстке/UI — сверяйся с `GUIDEBOOK.md`.** Это источник правды по
дизайн-системе: токены, типографика, лейаут-каркас `.page`, глобальные компоненты, правила.
Новый UI строим на существующих классах/токенах, не изобретаем свой лейаут и не хардкодим
цвета/отступы. Если гайдбук чего-то не покрывает — сначала дополняем гайдбук, потом код.

## Наблюдаемость и логирование (где что)

Чтобы не искать заново — куда логировать и как смотреть.

- **AI-вызовы (любой провайдер)** идут через `ai/observe.log_ai_call(...)` (вызывается внутри
  `ai/deepseek.py` и `ai/cloudflare.py`). Он делает сразу три вещи:
  1) консольный лог (логгер `easy_week.ai`) → journald;
  2) строку JSONL в файл-за-день `backend/data/ai-logs/ai-YYYY-MM-DD.jsonl` — для анализа.
     Поля: ts, provider, model, label, `ok` (true/false), `duration_ms`, usage/кэш, messages,
     response (на успехе) либо `error`+`attempt` (на неудачной попытке), плюс корреляция запроса —
     `conversation_id`/`plan_id`/`dish_id`/`endpoint`/`action`. Контекст корреляции выставляет
     роутер через `observe.set_ai_context(...)` (contextvars, без протаскивания через гейты);
  3) Prometheus-счётчики `easyweek_ai_calls_total` / `easyweek_ai_tokens_total{kind=…}` /
     `easyweek_ai_errors_total`.
  **Новый AI-вызов логируется сам, если идёт через хелперы deepseek/cloudflare.** Не дублировать.
- **Обычные логи приложения:** `logging.getLogger("easy_week.<модуль>")` (INFO) → stdout → journald
  (`journalctl -u easy-week-backend`). Свой логгер не изобретать.
- **Метрики:** `/metrics` (prometheus-fastapi-instrumentator). На Пае проброшен nginx: `:8080/metrics`.
- **Стек мониторинга — в `monitoring/`** (Prometheus + Loki + Promtail + Grafana). Локально —
  через Docker из `docker/` (см. ниже), на Пае native (`monitoring/install-pi.sh`).
  Grafana на Пае: `http://192.168.1.230:3002`. Логи смотреть в Grafana → Explore → Loki:
  `{job="easy-week-ai"}` (AI-вызовы) или `{job="easy-week-backend"}` (сервис).

## Docker (локальный запуск всего из одного места)

Все docker-конфиги — в **`docker/`** (compose + Dockerfile'ы). Запускать оттуда: `cd docker`.
- `docker compose up -d --build` — бэкенд + фронт (порт 8080).
- `docker compose --profile monitoring up -d --build` — то же + Prometheus/Loki/Promtail/Grafana.
- Dev-оверрайд (`docker-compose.override.yml`) подхватывается сам: бэкенд `--reload` + монтирование кода.

Один compose-проект `easy-week`: приложение и мониторинг в общей сети и на одном томе
`easy-week_ewdata` (AI-логи), поэтому мониторинг просто профиль — без external-сети/тома.
Build-контексты остаются `backend/` и `frontend/` (там `requirements.txt`/`package.json`),
Dockerfile'ы вынесены в `docker/*.Dockerfile`. Конфиги самих сервисов мониторинга
(`*.docker.yml`, дашборды) остаются в `monitoring/` рядом с нативным Пай-деплоем и монтируются
из compose. На Пае — по-прежнему native (systemd), не через этот compose (см. `deploy/README.md`).

## Деплой на Raspberry Pi (после каждого коммита)

Каждый коммит **сразу катим на Пай**. Деплой — через git + SSH:

```bash
git push                                   # запушить коммит(ы)
ssh pi5 'cd ~/easy-week && bash deploy/update.sh'
```

- SSH-хост — алиас **`pi5`** (пользователь `pashtitto`, каталог `~/easy-week`), ключ настроен —
  пароль не нужен. Прямой `pashtitto@192.168.1.230` без ключа не пускает — используем `pi5`.
- `deploy/update.sh` делает всё: `git pull --ff-only` → пересборка бэка (venv+pip) и фронта
  (`npm ci && npm run build`) → `systemctl restart easy-week-backend` + `nginx reload`.
- Локально в сети: `http://192.168.1.230:8080`. Логи: `ssh pi5 'journalctl -u easy-week-backend -f'`.
- Полное описание (первичная настройка, Cloudflare Tunnel) — `deploy/README.md`.

## Прочее
- Бэклог и хотелки — в `ROADMAP.md`. Мониторинг — в `monitoring/README.md`. Docker — в `docker/README.md`.
- Единицы ингредиентов задаём у источника: только `г` / `мл`, `шт` — редко (штучное).
