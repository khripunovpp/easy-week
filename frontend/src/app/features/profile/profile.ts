import { Component, inject } from '@angular/core';
import { Gender, Preferences, RecipeModel, ThemeMode } from '../../services/preferences';

@Component({
  selector: 'ew-profile',
  templateUrl: './profile.html',
  styleUrl: './profile.scss',
})
export class ProfilePage {
  readonly prefs = inject(Preferences);

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
    { value: 'cloudflare', label: 'Cloudflare' },
  ];
}
