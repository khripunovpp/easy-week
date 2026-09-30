import { Injectable, computed, inject, signal } from '@angular/core';
import { AppSettings, EasyWeekApi, ModelDefaults, ModelTask, TaskModels } from './api';
import { ALL_MODELS, RecipeModel } from './preferences';

// Кэш последних серверных настроек — чтобы дефолты были верными сразу при старте (до ответа
// сервера) и офлайн. Источник правды — сервер (/api/settings), кэш только подсказка.
const CACHE_KEY = 'ew.modelDefaults';
const TASKS_KEY = 'ew.taskModels';
// Старый единый выбор модели из профиля (до серверных настроек) — разово переносим на сервер.
const LEGACY_KEY = 'ew.recipeModel';

// Встроенные дефолты (как на бэке, пока настройки не сохранены): покупки/предпочтения — Cloudflare.
const BUILTIN: ModelDefaults = {
  chat: 'deepseek',
  recipe: 'deepseek',
  shopping: 'cloudflare',
  cooking: 'deepseek',
  prefs: 'cloudflare',
  summary: 'cloudflare',
};

// Карта «задача → модели» как на бэке (services/settings.TASK_MODELS) — пока сервер не ответил.
// Cloudflare/OpenRouter (дешёвые) не годятся для развёрнутых рецептов и плана готовки.
const FULL: RecipeModel[] = ['deepseek', 'gemini', 'anthropic'];
const BUILTIN_TASKS: TaskModels = {
  chat: [...FULL, 'cloudflare', 'openrouter'],
  recipe: FULL,
  shopping: [...FULL, 'cloudflare', 'openrouter'],
  cooking: FULL,
  prefs: ['cloudflare', 'openrouter', 'deepseek', 'gemini'],
  summary: ['cloudflare', 'openrouter', 'deepseek', 'gemini'],
};

function isModel(v: unknown): v is RecipeModel {
  return typeof v === 'string' && (ALL_MODELS as string[]).includes(v);
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

// Модели по умолчанию для каждой задачи — общие для всех устройств семьи (хранятся на сервере).
// Страницы (чат, рецепт, готовка, покупки) стартуют с этих значений, но могут выбрать другую
// модель локально — это не меняет настройки. Что вообще можно выбрать для задачи — карта
// taskModels с сервера: выпадашки строятся по ней (modelsFor).
@Injectable({ providedIn: 'root' })
export class ModelSettings {
  private readonly api = inject(EasyWeekApi);

  readonly models = signal<ModelDefaults>(this.readCache());
  // Карта «задача → допустимые модели» (сервер; кэш на старт/офлайн).
  readonly taskModels = signal<TaskModels>(this.readTasks());
  // Конкретные модели за ключами (deepseek → deepseek-chat …) — для подписей в выпадашках.
  // Кэшируем, чтобы подписи были сразу при старте/офлайн.
  readonly names = signal<Record<string, string>>(this.readNames());
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
  set(task: ModelTask, model: RecipeModel): void {
    const next = { ...this.models(), [task]: model };
    this.models.set(next);
    this.writeCache(next);
    this.api.putSettings(next).subscribe({ next: (s) => this.apply(s) });
  }

  /** Модели, которые можно выбрать для задачи (порядок — как в выпадашке). */
  modelsFor(task: ModelTask): RecipeModel[] {
    return this.taskModels()[task] ?? BUILTIN_TASKS[task];
  }

  /** Реактивный вариант modelsFor — для шаблонов и computed. */
  modelsForSignal(task: ModelTask) {
    return computed(() => this.modelsFor(task));
  }

  /** Конкретная модель за ключом («» — пока неизвестна). */
  modelId(key: string): string {
    return this.names()[key] ?? '';
  }

  /** Бесплатная ли модель: Cloudflare (свободные нейроны) или «:free»-модель OpenRouter. */
  isFree(key: string): boolean {
    return key === 'cloudflare' || this.modelId(key).endsWith(':free');
  }

  private readNames(): Record<string, string> {
    try {
      return JSON.parse(localStorage.getItem('ew.modelNames') || '{}');
    } catch {
      return {};
    }
  }

  private apply(s: AppSettings): void {
    if (s.modelNames) {
      this.names.set(s.modelNames);
      try {
        localStorage.setItem('ew.modelNames', JSON.stringify(s.modelNames));
      } catch {
        /* приватный режим — просто без кэша */
      }
    }
    if (s.taskModels) {
      const tasks = pickTasks(s.taskModels);
      this.taskModels.set(tasks);
      try {
        localStorage.setItem(TASKS_KEY, JSON.stringify(tasks));
      } catch {
        /* не критично */
      }
    }
    const next = { ...BUILTIN };
    for (const t of Object.keys(BUILTIN) as ModelTask[]) {
      if (isModel(s.models?.[t])) next[t] = s.models[t];
    }
    this.models.set(next);
    this.loaded.set(true);
    this.writeCache(next);
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
      this.modelsFor(t).includes(legacy) ? legacy : BUILTIN[t];
    const seeded: ModelDefaults = {
      chat: forTask('chat'),
      recipe: forTask('recipe'),
      shopping: 'cloudflare',
      cooking: forTask('cooking'),
      prefs: 'cloudflare',
      summary: 'cloudflare',
    };
    this.models.set(seeded);
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
    try {
      const raw = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null') as Partial<ModelDefaults> | null;
      const out = { ...BUILTIN };
      if (raw) for (const t of Object.keys(BUILTIN) as ModelTask[]) if (isModel(raw[t])) out[t] = raw[t];
      return out;
    } catch {
      return { ...BUILTIN };
    }
  }

  private readTasks(): TaskModels {
    try {
      return pickTasks(JSON.parse(localStorage.getItem(TASKS_KEY) || 'null'));
    } catch {
      return { ...BUILTIN_TASKS };
    }
  }

  private writeCache(m: ModelDefaults): void {
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(m));
    } catch {
      /* не критично */
    }
  }
}
