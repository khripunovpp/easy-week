# Easy Week — правила проекта

## Роутинг AI-моделей (главное правило)

**Каждую AI-задачу делает модель, выбранная пользователем.** Модели: **DeepSeek**
(`deepseek-chat`), **Gemini** (`gemini-flash-latest`), **Claude** (`claude-opus-4-8`, Anthropic API —
нужен баланс кредитов в console.anthropic.com), **Cloudflare** (mistral-пайплайн), **OpenRouter**
(`OPENROUTER_MODEL`, по умолчанию бесплатная `nvidia/nemotron-3-super-120b-a12b:free` — проба
дешёвых моделей на служебных задачах).

**Модели по умолчанию — по задачам, на сервере** (общие для всех устройств семьи):
Профиль → «Модели по умолчанию» (`/settings/models`), `GET/PUT /api/settings`,
файл `backend/data/settings.json` (`services/settings.py`, атомарная запись, в `deploy/backup.sh`).

| Задача (`task`) | Что входит | Встроенный дефолт |
|---|---|---|
| `chat` — «Чат и план» | генерация плана, правки в чате, ответы в обсуждении | `RECIPE_MODEL_DEFAULT` |
| `recipe` — «Рецепты» | рецепт блюда (открыть/перегенерировать), догенерация рецептов для покупок/PDF/готовки | `RECIPE_MODEL_DEFAULT` |
| `shopping` — «Список покупок» | нормализация списка покупок (GET и ↻) | `cloudflare` |
| `cooking` — «План готовки» | единый план готовки | `RECIPE_MODEL_DEFAULT` |
| `prefs` — «Предпочтения из чата» | фоновое извлечение вкусов из сообщений чата (`ai/prefs.py`) | `cloudflare` |

**Карта «задача → какие модели можно выбрать»** — `TASK_MODELS` в `services/settings.py`
(единственный источник; фронт получает её из `GET /api/settings` → `taskModels` и строит
выпадашки только из неё):

| Задача | DeepSeek | Gemini | Claude | Cloudflare | OpenRouter |
|---|---|---|---|---|---|
| `chat` | ✓ | ✓ | ✓ | ✓ (свой пайплайн) | ✓ (проба) |
| `recipe` | ✓ | ✓ | ✓ | ✗ | ✗ |
| `cooking` | ✓ | ✓ | ✓ | ✗ | ✗ |
| `shopping` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `prefs` | ✓ | ✓ | ✗ (дорого/лимит) | ✓ | ✓ |

Дешёвые модели (Cloudflare, OpenRouter) плохо пишут длинный связный JSON — развёрнутые рецепты
и план готовки им не предлагаем. Правила карты: `PUT /api/settings` с неподходящей парой → 422;
`get_models()` игнорирует сохранённое раньше неподходящее значение; `resolve_key(model, task)`
явную неподходящую модель (старый клиент, внутренний шаг вроде правки рецепта моделью чата)
заменяет дефолтом задачи с warning в лог — это политика выбора, а не фолбэк по сбою.
Уже сгенерированные варианты рецепта/готовки «запрещённой» моделью остаются открываемыми
(переключение на существующий вариант карте не подчиняется, новых такой моделью не делаем).

Страница может выбрать другую модель локально (не меняя настройки): чат — override на этот чат,
рецепт/готовка — выпадашка вариантов, покупки — выпадашка в шапке (для ↻). Запрос без модели
(`recipeModel: ""`) → бэк берёт дефолт задачи: `gate_for(model, task)` / `resolve_key(model, task)`
в `ai/gates.py`. Явно переданная модель всегда важнее дефолта.

**Фолбэков между моделями НЕТ.** Выбранная модель либо отвечает, либо кидает `AIError` →
роутер отдаёт 502 / `event: error`, и фронт просит переключить модель или собрать план заново.

**Список покупок** — детерминированная база из ингредиентов + нормализация моделью задачи
`shopping` (по умолчанию Cloudflare mistral `@cf/mistralai/mistral-small-3.1-24b-instruct`
со строгой json_schema; DeepSeek/Gemini/Claude — JSON-режим, форма ответа в `SHOP_SYSTEM`).
Сбой нормализации в GET — отдаём базу без кэша (это не подмена модели), в ↻ — 502.
Фоновое извлечение предпочтений (`ai/prefs.py`) — модель задачи `prefs` (Cloudflare — со схемой,
остальные — JSON-режим и few-shot). Все AI-контексты выбираются в настройках; вне выбора остаются
только под-модели внутри Cloudflare-пайплайна (`CF_MODEL*` в .env — это внутренности одного
провайдера, а не отдельная задача).

Провайдеры логируются с меткой: `AI → DeepSeek · …` / `AI → Gemini · …` / `AI → Cloudflare · …` /
`AI → OpenRouter · …`.

**Озвучка шагов (TTS)** — `ai/tts.py` + `GET /api/tts?text=…` (`routers/tts.py`): OpenRouter
`POST /audio/speech`, бесплатная `fish-audio/s2.1-pro-free:free` (русский, mp3; модель/голос —
`OPENROUTER_TTS_MODEL` / `OPENROUTER_TTS_VOICE`). В настройки не выведено — провайдер один.
Кэш `data/tts/<sha1(модель·голос|текст)>.mp3` (не бэкапим), параллельные запросы одного шага
склеиваются, `Cache-Control: private, max-age=30d`. Фронт: `ew-tts-btn` (`shared/tts-btn.ts`) +
общий плеер `TtsPlayer` — `audio.src` + `play()` синхронно в тапе (иначе iOS не играет), поэтому
GET с текстом в query, а не POST. TTS-модели OpenRouter в общем каталоге `/models` не видны
(`deepgram/flux-tts:free` — только английский, требует `voice=flux-*-en`).

### Архитектура (гейты)
Наглядная схема (потоки + классы + матрица задач) — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Каждый провайдер — подкласс `ModelGate` (`ai/base.py`): `complete_json` (шаблонный метод с
ретраями/логом), хуки `_request_json`/`stream_json`/`call_tools`. Реестр и выбор — `ai/gates.py`
(`gate_for(model_key, task)` — пустой ключ → дефолт задачи). `ai/planner.py` роутит по выбранной модели, без `try/except → другой провайдер`.
- DeepSeek/Gemini/Claude — план одним запросом; Cloudflare — пайплайн меню→спеки→валидатор.
- Правки: DeepSeek — function calling; Gemini/Claude/Cloudflare — structured actions.
- Gemini: `thinkingBudget=0` (рецептам reasoning не нужен, иначе обрывает JSON).
- Claude: system — отдельным полем; температуру не шлём (Opus 4.8/4.7 её отвергают); JSON-режима
  нет — просим строгий JSON в промпте и лениво парсим (снимаем ```-ограждение).
- Новый ключ Google не даёт `gemini-2.5-flash` — используем алиас `gemini-flash-latest`.
- OpenRouter (`ai/openrouter.py`): OpenAI-совместимый, `response_format: json_object` +
  `reasoning.enabled=false` (иначе reasoning съедает max_tokens и рвёт JSON), ленивый парсер
  `loads_lenient` (общий с Claude, в `ai/base.py`); стрима/tools нет. Ошибка может прийти телом
  при 200 — проверяем `error`. Бесплатные модели: ~20 запр./мин и дневной лимит, upstream-429
  бывает часто — это `AIError`, а не повод для фолбэка. `respan/span-01-lite:free` — модель
  «decisions» (скоринг), chat/completions не умеет — для генерации не годится.

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
- **Метрики:** `/metrics` (prometheus-fastapi-instrumentator). На Пае проброшен nginx: `:8080/metrics` (только LAN/tailnet/localhost).
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
- **HTTPS/офлайн PWA:** service worker не регистрируется по LAN-http → офлайн не работает. Решение —
  **Tailscale Funnel** (`https://<pi>.<tailnet>.ts.net`, без домена), см. `deploy/README.md` шаг 6.
  Устанавливать PWA нужно с ts.net-адреса (SW/кэш привязаны к origin).
- Полное описание (первичная настройка, Tailscale/Cloudflare) — `deploy/README.md`.

## Прочее
- **Вход по общему паролю:** `APP_PASSWORD` (+ опц. `APP_SECRET`) в `backend/.env`, не в репо; пусто — вход выключен. Бэк — `backend/app/auth.py` (middleware на `/api/*`), фронт — `/login` + `services/auth*.ts`. Подробно — `deploy/README.md`.
- Бэклог и хотелки — в `ROADMAP.md`. Мониторинг — в `monitoring/README.md`. Docker — в `docker/README.md`.
- Единицы ингредиентов задаём у источника: только `г` / `мл`, `шт` — редко (штучное).
