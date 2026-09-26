import { Component, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterOutlet, RouterLink, RouterLinkActive } from '@angular/router';
import { filter, map } from 'rxjs';
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

  // На экране входа таб-бар скрыт: вкладки всё равно увели бы обратно на /login.
  private readonly router = inject(Router);
  readonly isLogin = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => e.urlAfterRedirects.startsWith('/login')),
    ),
    { initialValue: false },
  );
}
