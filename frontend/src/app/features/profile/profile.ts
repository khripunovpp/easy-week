import { Component, inject, signal } from '@angular/core';
import { EasyWeekApi, FoodPrefs, LimitsStatus } from '../../services/api';
import { Gender, Preferences, RecipeModel, ThemeMode } from '../../services/preferences';

type PrefKind = 'dislikes' | 'likes';

@Component({
  selector: 'ew-profile',
  templateUrl: './profile.html',
  styleUrl: './profile.scss',
})
export class ProfilePage {
  readonly prefs = inject(Preferences);
  private readonly api = inject(EasyWeekApi);

  // Остаток дневного лимита Claude (планы/рецепты) — грузим при открытии профиля.
  readonly limits = signal<LimitsStatus | null>(null);
  // Пищевые предпочтения (что любит / не любит) — учитываются во всех генерациях.
  readonly foodPrefs = signal<FoodPrefs>({ dislikes: [], likes: [] });

  constructor() {
    this.api.limits().subscribe({ next: (l) => this.limits.set(l) });
    this.api.getPreferences().subscribe({ next: (p) => this.foodPrefs.set(p) });
  }

  private savePrefs(next: FoodPrefs): void {
    this.foodPrefs.set(next); // оптимистично
    this.api.setPreferences(next).subscribe({ next: (p) => this.foodPrefs.set(p) });
  }

  addPref(kind: PrefKind, value: string): void {
    const v = value.trim();
    if (!v) return;
    const cur = this.foodPrefs();
    if (cur[kind].some((x) => x.toLowerCase() === v.toLowerCase())) return;
    this.savePrefs({ ...cur, [kind]: [...cur[kind], v] });
  }

  removePref(kind: PrefKind, item: string): void {
    const cur = this.foodPrefs();
    this.savePrefs({ ...cur, [kind]: cur[kind].filter((x) => x !== item) });
  }

  readonly themeOptions: { value: ThemeMode; label: string }[] = [
    { value: 'system', label: 'Система' },
    { value: 'light', label: 'Светлая' },
    { value: 'dark', label: 'Тёмная' },
  ];

  readonly genderOptions: { value: Gender; label: string }[] = [
    { value: 'f', label: 'Женский' },
    { value: 'm', label: 'Мужской' },
  ];

  readonly recipeModelOptions: { value: RecipeModel; label: string }[] = [
    { value: 'deepseek', label: 'DeepSeek' },
    { value: 'gemini', label: 'Gemini' },
    { value: 'anthropic', label: 'Claude' },
    { value: 'cloudflare', label: 'Cloudflare' },
  ];
}
