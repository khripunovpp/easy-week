import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AuthService } from '../../services/auth';
import { Gender, Preferences, ThemeMode } from '../../services/preferences';

// Профиль: настройки устройства (тема, пол ассистента) + меню под-экранов —
// «Модели по умолчанию» (/settings/models) и «Предпочтения» (/preferences).
@Component({
  selector: 'ew-profile',
  imports: [RouterLink],
  templateUrl: './profile.html',
  styleUrl: './profile.scss',
})
export class ProfilePage {
  readonly prefs = inject(Preferences);
  // Выход показываем, только если вход по паролю включён на сервере.
  readonly auth = inject(AuthService);

  logout(): void {
    void this.auth.logout();
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
}
