# Easy Week — правила проекта

## Роутинг AI-моделей (главное правило)

**Рецепты придумывает модель, выбранная пользователем.** Выбор — в профиле (глобальный дефолт)
и в чате (override только для этого чата, профиль не трогает). Три модели: **DeepSeek**
(`deepseek-chat`), **Gemini** (`gemini-flash-latest`), **Cloudflare** (mistral-пайплайн).
Сюда входит генерация плана (блюда + короткие шаги) и развёрнутые пошаговые рецепты —
всё той же выбранной моделью (деталь = текущая модель чата, не пиннится к модели плана).

**Фолбэков между моделями НЕТ.** Выбранная модель либо отвечает, либо кидает `AIError` →
роутер отдаёт 502 / `event: error`, и фронт просит переключить модель или собрать план заново.

**Вспомогательные задачи — всегда Cloudflare Workers AI** (mistral,
`@cf/mistralai/mistral-small-3.1-24b-instruct`): нормализация списка покупок. Не участвуют
в пользовательском выборе. (База списка покупок собирается детерминированно из ингредиентов.)

Провайдеры логируются с меткой: `AI → DeepSeek · …` / `AI → Gemini · …` / `AI → Cloudflare · …`.

### Архитектура (гейты)
Каждый провайдер — подкласс `ModelGate` (`ai/base.py`): `complete_json` (шаблонный метод с
ретраями/логом), хуки `_request_json`/`stream_json`/`call_tools`. Реестр и выбор — `ai/gates.py`
(`gate_for(model_key)`). `ai/planner.py` роутит по выбранной модели, без `try/except → другой провайдер`.
- DeepSeek/Gemini — план одним запросом; Cloudflare — пайплайн меню→спеки→валидатор.
- Правки: DeepSeek — function calling; Gemini/Cloudflare — structured actions.
- Gemini: `thinkingBudget=0` (рецептам reasoning не нужен, иначе обрывает JSON).
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
  2) строку JSONL в файл-за-день `backend/data/ai-logs/ai-YYYY-MM-DD.jsonl`
     (ts, provider, model, label, полный usage/кэш, messages, response) — для анализа;
  3) Prometheus-счётчики `easyweek_ai_calls_total` / `easyweek_ai_tokens_total{kind=…}`.
  **Новый AI-вызов логируется сам, если идёт через хелперы deepseek/cloudflare.** Не дублировать.
- **Обычные логи приложения:** `logging.getLogger("easy_week.<модуль>")` (INFO) → stdout → journald
  (`journalctl -u easy-week-backend`). Свой логгер не изобретать.
- **Метрики:** `/metrics` (prometheus-fastapi-instrumentator). На Пае проброшен nginx: `:8080/metrics`.
- **Стек мониторинга — в `monitoring/`** (Prometheus + Loki + Promtail + Grafana). Локально Docker
  (`monitoring/docker-compose.yml`), на Пае native (`monitoring/install-pi.sh`).
  Grafana на Пае: `http://192.168.1.230:3002`. Логи смотреть в Grafana → Explore → Loki:
  `{job="easy-week-ai"}` (AI-вызовы) или `{job="easy-week-backend"}` (сервис).

## Прочее
- Бэклог и хотелки — в `ROADMAP.md`. Мониторинг — в `monitoring/README.md`.
- Единицы ингредиентов задаём у источника: только `г` / `мл`, `шт` — редко (штучное).
