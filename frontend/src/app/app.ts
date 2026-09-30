import { Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterOutlet, RouterLink, RouterLinkActive } from '@angular/router';
import { filter, map } from 'rxjs';
import { Preferences } from './services/preferences';
import { PwaUpdate } from './services/pwa-update';
import { TtsPanel } from './shared/tts-panel';
import { TtsPlayer } from './shared/tts-player';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, TtsPanel],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  // Инициализируем настройки на старте — тема применяется сразу (data-theme + theme-color).
  private readonly prefs = inject(Preferences);
  // Авто-обновление PWA: подхватывает новую версию без ручного сброса кэша.
  private readonly pwa = inject(PwaUpdate);

  private readonly router = inject(Router);
  // Текущий URL после редиректов — для таб-бара.
  private readonly url = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => e.urlAfterRedirects),
    ),
    { initialValue: '' },
  );
  // На экране входа таб-бар скрыт: вкладки всё равно увели бы обратно на /login.
  readonly isLogin = computed(() => this.url().startsWith('/login'));
  // Нижняя панель озвучки шага — пока есть текущий шаг (не на экране входа).
  private readonly tts = inject(TtsPlayer);
  readonly playerOn = computed(() => this.tts.visible() && !this.isLogin());
  // Вкладка «Профиль» подсвечена и на его под-экранах (модели по умолчанию, предпочтения).
  readonly profileActive = computed(() =>
    ['/profile', '/settings', '/preferences'].some((p) => this.url().startsWith(p)),
  );
}
