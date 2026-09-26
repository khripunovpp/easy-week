import { Injectable, effect, signal } from '@angular/core';

export type ThemeMode = 'system' | 'light' | 'dark';
export type Gender = 'f' | 'm';
export type RecipeModel = 'deepseek' | 'gemini' | 'cloudflare' | 'anthropic';

// Модели рецептов: ключ → человекочитаемое имя (совпадает с provider плана на бэке).
export const MODEL_LABELS: Record<RecipeModel, string> = {
  deepseek: 'DeepSeek',
  gemini: 'Gemini',
  anthropic: 'Claude',
  cloudflare: 'Cloudflare',
};
export const ALL_MODELS: RecipeModel[] = ['deepseek', 'gemini', 'anthropic', 'cloudflare'];

// Провайдер плана (человекочитаемый, из бэка: «DeepSeek»/«Claude»…) → ключ модели.
// Пусто, если не распознан. Обратный маппинг к MODEL_LABELS.
export function providerToModel(provider: string): RecipeModel | '' {
  const p = (provider || '').trim();
  const byLabel = (Object.keys(MODEL_LABELS) as RecipeModel[]).find((k) => MODEL_LABELS[k] === p);
  if (byLabel) return byLabel;
  const low = p.toLowerCase();
  return (ALL_MODELS as string[]).includes(low) ? (low as RecipeModel) : '';
}

const THEME_KEY = 'ew.theme';
const GENDER_KEY = 'ew.gender';
const BAR_LIGHT = '#fbe9e1';
const BAR_DARK = '#201a17';

// Настройки устройства (тема, пол ассистента) — храним в localStorage, авторизации нет.
// Модели по умолчанию — общие для семьи, на сервере: см. services/model-settings.ts.
@Injectable({ providedIn: 'root' })
export class Preferences {
  readonly theme = signal<ThemeMode>(this.readTheme());
  readonly gender = signal<Gender>(this.readGender());

  private readonly darkMql = window.matchMedia('(prefers-color-scheme: dark)');

  constructor() {
    // Применяем сразу (без мигания при старте) и реактивно на смену сигнала.
    this.applyTheme(this.theme());
    effect(() => {
      const t = this.theme();
      this.applyTheme(t);
      localStorage.setItem(THEME_KEY, t);
    });
    effect(() => localStorage.setItem(GENDER_KEY, this.gender()));
    // При теме «Система» следим за системной сменой светлая/тёмная.
    this.darkMql.addEventListener('change', () => {
      if (this.theme() === 'system') this.applyTheme('system');
    });
  }

  setTheme(mode: ThemeMode): void {
    this.theme.set(mode);
  }
  setGender(g: Gender): void {
    this.gender.set(g);
  }

  private applyTheme(mode: ThemeMode): void {
    const root = document.documentElement;
    if (mode === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);
    const dark = mode === 'dark' || (mode === 'system' && this.darkMql.matches);
    this.setBarColor(dark ? BAR_DARK : BAR_LIGHT);
  }

  private setBarColor(color: string): void {
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.setAttribute('name', 'theme-color');
      document.head.appendChild(meta);
    }
    meta.setAttribute('content', color);
  }

  private readTheme(): ThemeMode {
    const v = localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
  }
  private readGender(): Gender {
    return localStorage.getItem(GENDER_KEY) === 'm' ? 'm' : 'f';
  }
}
