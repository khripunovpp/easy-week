import { Injectable, computed, inject, signal } from '@angular/core';
import { AppSettings, CatalogModel, EasyWeekApi, ModelDefaults, ModelRef, ModelTask, TaskModels } from './api';
import { ALL_MODELS, RecipeModel } from './preferences';

// Кэш последних серверных настроек — чтобы дефолты были верными сразу при старте (до ответа
// сервера) и офлайн. Источник правды — сервер (/api/settings), кэш только подсказка.
const CACHE_KEY = 'ew.modelDefaults';
const TASKS_KEY = 'ew.taskModels';
const CATALOG_KEY = 'ew.modelCatalog';
// Старый единый выбор модели из профиля (до серверных настроек) — разово переносим на сервер.
const LEGACY_KEY = 'ew.recipeModel';

// Встроенные дефолты (как на бэке, пока настройки не сохранены): покупки/фон — Cloudflare.
const BUILTIN: ModelDefaults = {
  chat: 'deepseek',
  recipe: 'deepseek',
  shopping: 'cloudflare',
  cooking: 'deepseek',
  prefs: 'cloudflare',
  summary: 'cloudflare',
  fix: 'deepseek',
};

// Карта «задача → модели» как на бэке (services/settings.TASK_MODELS) — пока сервер не ответил.
// Cloudflare/OpenRouter (дешёвые) не годятся для развёрнутых рецептов и плана готовки.
const FULL: RecipeModel[] = ['deepseek', 'gemini', 'anthropic'];
const BUILTIN_TASKS: TaskModels = {
  chat: [...FULL, 'cloudflare', 'openrouter'],
  recipe: FULL,
  shopping: [...FULL, 'cloudflare'],
  cooking: FULL,
  prefs: ['cloudflare', 'openrouter', 'deepseek', 'gemini'],
  summary: ['cloudflare', 'openrouter', 'deepseek', 'gemini'],
  // Правка рецепта — короткий ответ-правки; Cloudflare сдвигал номера шагов — не предлагаем.
  fix: [...FULL, 'openrouter'],
};

/** Ссылка «провайдер[:id]» → [провайдер, id или ""] (id OpenRouter содержит «:» — режем по первому). */
export function splitRef(ref: string): [string, string] {
  const i = (ref || '').indexOf(':');
  return i < 0 ? [(ref || '').toLowerCase(), ''] : [ref.slice(0, i).toLowerCase(), ref.slice(i + 1)];
}

function isModel(v: unknown): v is RecipeModel {
  return typeof v === 'string' && (ALL_MODELS as string[]).includes(v);
}

function isRef(v: unknown): v is ModelRef {
  return typeof v === 'string' && isModel(splitRef(v)[0]);
}

function pickTasks(raw: Partial<Record<ModelTask, unknown>> | null | undefined): TaskModels {
  const out: TaskModels = { ...BUILTIN_TASKS };
  if (!raw) return out;
  for (const t of Object.keys(BUILTIN_TASKS) as ModelTask[]) {
    const list = raw[t];
    if (Array.isArray(list)) {
      const models = list.filter(isModel);
      if (models.length) out[t] = models;
    }
  }
  return out;
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const v = JSON.parse(localStorage.getItem(key) || 'null');
    return (v ?? fallback) as T;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, v: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(v));
  } catch {
    /* приватный режим — просто без кэша */
  }
}

// Модели по умолчанию для каждой задачи — общие для всех устройств семьи (хранятся на сервере).
// Значение задачи — ссылка «провайдер» или «провайдер:id» (конкретная модель; выбор — в настройках,
// выпадашки сгруппированы по провайдерам). Страницы (чат, рецепт, готовка, покупки) работают на
// уровне провайдера (`models()`): выбранный там провайдер бэк дополняет моделью задачи из
// настроек, если провайдер тот же (`refFor` — чтобы подпись показывала ту же модель).
@Injectable({ providedIn: 'root' })
export class ModelSettings {
  private readonly api = inject(EasyWeekApi);

  /** Полные ссылки на модели по задачам (как на сервере). */
  readonly refs = signal<ModelDefaults>(this.readCache());
  /** Провайдеры по задачам — для страниц, выбирающих на уровне провайдера. */
  readonly models = computed<Record<ModelTask, RecipeModel>>(() => {
    const out = {} as Record<ModelTask, RecipeModel>;
    for (const t of Object.keys(BUILTIN) as ModelTask[]) out[t] = splitRef(this.refs()[t])[0] as RecipeModel;
    return out;
  });
  // Карта «задача → допустимые провайдеры» (сервер; кэш на старт/офлайн).
  readonly taskModels = signal<TaskModels>(pickTasks(readJson(TASKS_KEY, null)));
  // Каталог конкретных моделей по провайдерам (модель по умолчанию — первая).
  readonly catalog = signal<Record<string, CatalogModel[]>>(readJson(CATALOG_KEY, {}));
  // Конкретные модели за ключами (deepseek → deepseek-chat …) — модель провайдера по умолчанию.
  readonly names = signal<Record<string, string>>(readJson('ew.modelNames', {}));
  // Настройки получены с сервера в этой сессии (иначе — кэш/встроенные дефолты).
  readonly loaded = signal(false);
  private inflight = false;

  constructor() {
    this.ensureLoaded();
  }

  // Подтянуть настройки, если ещё не получены (напр. первый запрос упал 401 до входа).
  ensureLoaded(): void {
    if (this.loaded() || this.inflight) return;
    this.refresh();
  }

  refresh(): void {
    this.inflight = true;
    this.api.getSettings().subscribe({
      next: (s) => {
        this.inflight = false;
        if (!s.initialized && this.migrateLegacy()) return;
        this.apply(s);
      },
      error: () => {
        this.inflight = false; // остаёмся на кэше; повторим при следующем ensureLoaded()
      },
    });
  }

  // Сменить модель по умолчанию для задачи (оптимистично, затем ответ сервера).
  set(task: ModelTask, ref: ModelRef): void {
    const next = { ...this.refs(), [task]: ref };
    this.refs.set(next);
    writeJson(CACHE_KEY, next);
    this.api.putSettings(next).subscribe({ next: (s) => this.apply(s) });
  }

  /** Провайдеры, которые можно выбрать для задачи (порядок — как в выпадашке). */
  modelsFor(task: ModelTask): RecipeModel[] {
    return this.taskModels()[task] ?? BUILTIN_TASKS[task];
  }

  /** Реактивный вариант modelsFor — для шаблонов и computed. */
  modelsForSignal(task: ModelTask) {
    return computed(() => this.modelsFor(task));
  }

  /** Конкретные модели провайдера (каталог; пусто, пока сервер не ответил). */
  catalogFor(provider: string): CatalogModel[] {
    return this.catalog()[provider] ?? [];
  }

  /** Модель провайдера по умолчанию: первая в каталоге или из modelNames. */
  defaultId(provider: string): string {
    return this.catalogFor(provider)[0]?.id ?? this.names()[provider] ?? '';
  }

  /** «провайдер[:id]» → «провайдер:id» с подставленной моделью по умолчанию (для сравнения). */
  fullRef(ref: ModelRef): string {
    const [p, id] = splitRef(ref);
    return `${p}:${id || this.defaultId(p)}`;
  }

  /** Ссылка для сохранения: модель по умолчанию — просто провайдер (переживёт смену .env). */
  makeRef(provider: string, id: string): ModelRef {
    return !id || id === this.defaultId(provider) ? provider : `${provider}:${id}`;
  }

  /** Модель, которой ответит провайдер на странице задачи: из настроек, если тот же провайдер. */
  refFor(task: ModelTask, provider: string): ModelRef {
    const ref = this.refs()[task];
    return splitRef(ref)[0] === provider ? ref : provider;
  }

  /** Конкретная модель за ссылкой/ключом («» — пока неизвестна). */
  modelId(ref: string): string {
    const [p, id] = splitRef(ref);
    if (id) return id;
    // Cloudflare: в names() — весь конвейер моделей; показываем главную (первая в каталоге).
    // Остальные: names() точнее (у Gemini там реальная версия за алиасом).
    return p === 'cloudflare' ? this.defaultId(p) : this.names()[p] || this.defaultId(p);
  }

  /** Бесплатная ли модель: Cloudflare (свободные нейроны) или «:free»-модель OpenRouter. */
  isFree(ref: string): boolean {
    return splitRef(ref)[0] === 'cloudflare' || this.modelId(ref).endsWith(':free');
  }

  private apply(s: AppSettings): void {
    if (s.modelNames) {
      this.names.set(s.modelNames);
      writeJson('ew.modelNames', s.modelNames);
    }
    if (s.catalog) {
      this.catalog.set(s.catalog);
      writeJson(CATALOG_KEY, s.catalog);
    }
    if (s.taskModels) {
      const tasks = pickTasks(s.taskModels);
      this.taskModels.set(tasks);
      writeJson(TASKS_KEY, tasks);
    }
    const next = { ...BUILTIN };
    for (const t of Object.keys(BUILTIN) as ModelTask[]) {
      if (isRef(s.models?.[t])) next[t] = s.models[t];
    }
    this.refs.set(next);
    this.loaded.set(true);
    writeJson(CACHE_KEY, next);
  }

  // Разовая миграция: настроек на сервере ещё нет, а на устройстве остался старый выбор
  // модели из профиля — переносим его на рецептные задачи (покупки — Cloudflare, как было).
  // true — миграция запущена (итог применит ответ PUT).
  private migrateLegacy(): boolean {
    let legacy: string | null = null;
    try {
      legacy = localStorage.getItem(LEGACY_KEY);
    } catch {
      /* localStorage недоступен — мигрировать нечего */
    }
    if (!isModel(legacy)) return false;
    // Старый выбор мог быть Cloudflare — на рецепты/готовку он не годится, берём встроенный.
    const forTask = (t: ModelTask): RecipeModel =>
      this.modelsFor(t).includes(legacy) ? legacy : (BUILTIN[t] as RecipeModel);
    const seeded: ModelDefaults = {
      chat: forTask('chat'),
      recipe: forTask('recipe'),
      shopping: 'cloudflare',
      cooking: forTask('cooking'),
      prefs: 'cloudflare',
      summary: 'cloudflare',
      fix: 'deepseek',
    };
    this.refs.set(seeded);
    this.api.putSettings(seeded).subscribe({
      next: (s) => {
        this.apply(s);
        try {
          localStorage.removeItem(LEGACY_KEY); // перенесли — больше не нужен
        } catch {
          /* не критично */
        }
      },
      error: () => this.loaded.set(false),
    });
    return true;
  }

  private readCache(): ModelDefaults {
    const raw = readJson<Partial<ModelDefaults> | null>(CACHE_KEY, null);
    const out = { ...BUILTIN };
    if (raw) for (const t of Object.keys(BUILTIN) as ModelTask[]) if (isRef(raw[t])) out[t] = raw[t] as ModelRef;
    return out;
  }
}
