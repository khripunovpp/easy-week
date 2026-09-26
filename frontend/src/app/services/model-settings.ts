import { Injectable, inject, signal } from '@angular/core';
import { AppSettings, EasyWeekApi, ModelDefaults, ModelTask } from './api';
import { ALL_MODELS, RecipeModel } from './preferences';

// Кэш последних серверных настроек — чтобы дефолты были верными сразу при старте (до ответа
// сервера) и офлайн. Источник правды — сервер (/api/settings), кэш только подсказка.
const CACHE_KEY = 'ew.modelDefaults';
// Старый единый выбор модели из профиля (до серверных настроек) — разово переносим на сервер.
const LEGACY_KEY = 'ew.recipeModel';

// Встроенные дефолты (как на бэке, пока настройки не сохранены): покупки — Cloudflare.
const BUILTIN: ModelDefaults = {
  chat: 'deepseek',
  recipe: 'deepseek',
  shopping: 'cloudflare',
  cooking: 'deepseek',
};

function isModel(v: unknown): v is RecipeModel {
  return typeof v === 'string' && (ALL_MODELS as string[]).includes(v);
}

// Модели по умолчанию для каждой задачи — общие для всех устройств семьи (хранятся на сервере).
// Страницы (чат, рецепт, готовка, покупки) стартуют с этих значений, но могут выбрать другую
// модель локально — это не меняет настройки.
@Injectable({ providedIn: 'root' })
export class ModelSettings {
  private readonly api = inject(EasyWeekApi);

  readonly models = signal<ModelDefaults>(this.readCache());
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

  private apply(s: AppSettings): void {
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
    const seeded: ModelDefaults = { chat: legacy, recipe: legacy, shopping: 'cloudflare', cooking: legacy };
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

  private writeCache(m: ModelDefaults): void {
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(m));
    } catch {
      /* не критично */
    }
  }
}
