# Архитектура: выбор и роутинг AI-моделей

Как в Easy Week работает выбор модели для рецептов. Правила — в [`CLAUDE.md`](../CLAUDE.md),
код — в `backend/app/ai/`. Наглядная интерактивная версия этой схемы — артефакт (ссылку см. в задаче).

**Главное:** три провайдера спрятаны за общим интерфейсом `ModelGate`. Пользователь выбирает
модель для рецептов; выбранная модель либо отвечает, либо честно падает с `AIError` — **тихого
перехода на другую модель нет (фолбэков нет)**.

Провайдеры (цвет = провайдер на схемах):
- 🔵 **DeepSeek** — `deepseek-chat`
- 🟣 **Gemini** — `gemini-flash-latest`
- 🟠 **Cloudflare** — Workers AI (mistral / llama)

## 1. Выбор модели и роутинг

Два независимых уровня выбора на фронте; переключение в чате **не** меняет дефолт профиля.
`recipeModel` едет в теле каждого запроса (как `gender`), на бэке `gate_for()` отдаёт нужный гейт.

```mermaid
flowchart TD
  subgraph FE["Фронт — выбор модели"]
    P["Профиль · Preferences.recipeModel<br/>localStorage ew.recipeModel · дефолт deepseek"]
    C["Чат · ChatStore.recipeModel<br/>override, профиль НЕ трогает"]
    P -. "инициализирует при newChat/load" .-> C
  end
  C -- "recipeModel в теле запроса" --> API["/chat · /chat/stream · /chat/edit<br/>/plans/../dishes/../details · /full"]
  API --> GF{{"gate_for(recipeModel)<br/>ai/gates.py"}}
  GF --> DS["DeepSeekGate<br/>stream ✓ · tools ✓"]
  GF --> GM["GeminiGate<br/>stream ✓ · tools ✗"]
  GF --> CF["CloudflareGate<br/>stream ✗ · tools ✗"]

  classDef ds fill:#dde7fc,stroke:#2f6bed,color:#12305f;
  classDef gm fill:#e7ddfb,stroke:#8b5cf6,color:#3a1f6b;
  classDef cf fill:#f8e3d1,stroke:#e8701d,color:#6b3410;
  classDef sys fill:#d3ede9,stroke:#0d9488,color:#0a4a44;
  class DS ds; class GM gm; class CF cf; class GF sys;
```

## 2. Классы: `ModelGate` (Strategy + Template Method)

Общий пайплайн — в базе (`ai/base.py`), провайдер-специфика — в хуках подклассов.
Шаблонный метод `complete_json`: guard «настроен?» → ретрай транзиентных ошибок →
`log_ai_call` → `(parsed, usage)`. `stream_json`/`call_tools` — переопределяемые
(по умолчанию `NotImplementedError`).

```mermaid
classDiagram
  class ModelGate {
    <<abstract>>
    +complete_json()  «шаблонный метод»
    +stream_json()
    +call_tools()
    #_request_json()  «хук, abstract»
    +configured
    +provider / key
  }
  ModelGate <|-- DeepSeekGate
  ModelGate <|-- GeminiGate
  ModelGate <|-- CloudflareGate
  class DeepSeekGate { OpenAI-совместимый · _request_json + stream_json + call_tools }
  class GeminiGate { REST · _request_json + stream_json · thinkingBudget=0 }
  class CloudflareGate { Workers AI json_schema · _request_json }
```

## 3. Что делает каждая модель по задачам

Выбранная модель обслуживает все рецептные задачи. Список покупок — исключение (всегда Cloudflare).

| Задача | 🔵 DeepSeek | 🟣 Gemini | 🟠 Cloudflare |
|---|---|---|---|
| **План** (блюда + короткие шаги) | один запрос — весь план | один запрос — весь план | **пайплайн:** меню (mistral) → спеки блюд (llama-8b, параллельно) → валидатор (mistral) |
| **Стриминг плана** | блюда по мере генерации (SSE) | блюда по мере генерации (SSE) | стрима нет: собирает целиком, отдаёт блюда теми же событиями |
| **Деталь рецепта** (ингредиенты + шаги) | один JSON-запрос текущей моделью чата — одинаково для всех трёх | ← | ← |
| **Правки плана** | function calling (tools) | structured actions | structured actions |
| **Список покупок** | всегда Cloudflare (mistral) — вспомогательная задача, в выборе не участвует | ← | ← |

Метка провайдера сохраняется у плана (`provider`) и у детали блюда (`detail_provider`) — показывается бейджем.

## 4. Если модель падает — ошибка, а не подмена

Главное следствие отказа от фолбэков: сбой виден, решает его пользователь.

```mermaid
flowchart LR
  F["Модель недоступна<br/>503 / 429 / таймаут /<br/>битый ответ после ретраев"] --> E["Гейт кидает AIError"]
  E --> R["Роутер → 502<br/>или SSE event: error"]
  R --> U["Фронт: «переключите модель<br/>или соберите план заново»"]
  classDef err fill:#f6dede,stroke:#d24545,color:#6e1f1f;
  class F,E,R,U err;
```

## 5. Наблюдаемость: один лог — три стока

Каждый успешный вызов проходит через `log_ai_call` (внутри `complete_json`/стрима — логируется сам).

```mermaid
flowchart TD
  L["log_ai_call(provider, model, label, …)"] --> A["Консоль → journald<br/>easy_week.ai"]
  L --> B["JSONL за день<br/>data/ai-logs/ai-*.jsonl"]
  L --> C2["Prometheus → Grafana<br/>easyweek_ai_*_total"]
  classDef sys fill:#d3ede9,stroke:#0d9488,color:#0a4a44;
  class L sys;
```

## Файлы

```
backend/app/ai/
  base.py        # AIError + ModelGate (шаблонный метод + хуки)
  gates.py       # реестр GATES + gate_for(model)
  deepseek.py    # DeepSeekGate
  gemini.py      # GeminiGate
  cloudflare.py  # CloudflareGate
  planner.py     # роутинг по выбранной модели, без фолбэков
  observe.py     # log_ai_call (консоль + JSONL + Prometheus)
```
