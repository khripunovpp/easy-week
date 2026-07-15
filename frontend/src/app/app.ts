import { Component, DestroyRef, inject } from '@angular/core';
import { RouterOutlet, RouterLink, RouterLinkActive } from '@angular/router';
import { Preferences } from './services/preferences';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  // Инициализируем настройки на старте — тема применяется сразу (data-theme + theme-color).
  private readonly prefs = inject(Preferences);

  constructor() {
    // Высота приложения = реально видимая область (visualViewport), а не layout-вьюпорт.
    // На iOS 100dvh/position:fixed прибиваются к БОЛЬШОМУ вьюпорту (без тулбара), из-за чего
    // низ уезжает под адресную строку и не доскроллить. visualViewport.height точен всегда
    // (тулбар, клавиатура) — пишем его в --app-h, body берёт эту высоту.
    const setH = () => {
      const h = window.visualViewport?.height ?? window.innerHeight;
      document.documentElement.style.setProperty('--app-h', `${Math.round(h)}px`);
    };
    setH();
    const vv = window.visualViewport;
    vv?.addEventListener('resize', setH);
    vv?.addEventListener('scroll', setH);
    window.addEventListener('resize', setH);
    inject(DestroyRef).onDestroy(() => {
      vv?.removeEventListener('resize', setH);
      vv?.removeEventListener('scroll', setH);
      window.removeEventListener('resize', setH);
    });
  }
}
