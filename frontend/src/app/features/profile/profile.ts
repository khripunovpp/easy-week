import { Component, inject, signal } from '@angular/core';
import { EasyWeekApi, LimitsStatus } from '../../services/api';
import { Gender, Preferences, RecipeModel, ThemeMode } from '../../services/preferences';

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

  constructor() {
    this.api.limits().subscribe({ next: (l) => this.limits.set(l) });
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
