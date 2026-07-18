import { Component, inject } from '@angular/core';
import { RouterOutlet, RouterLink, RouterLinkActive } from '@angular/router';
import { Preferences } from './services/preferences';
import { PwaUpdate } from './services/pwa-update';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  // Инициализируем настройки на старте — тема применяется сразу (data-theme + theme-color).
  private readonly prefs = inject(Preferences);
  // Авто-обновление PWA: подхватывает новую версию без ручного сброса кэша.
  private readonly pwa = inject(PwaUpdate);
}
