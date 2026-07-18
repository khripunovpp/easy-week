import { Injectable, inject } from '@angular/core';
import { SwUpdate, VersionReadyEvent } from '@angular/service-worker';
import { filter } from 'rxjs';

// Авто-обновление PWA: без этого service worker отдаёт закэшированную версию, пока
// пользователь сам не перезапустит приложение. Здесь: как только новая версия скачана —
// активируем её и перезагружаем; плюс проверяем обновления при старте и при возврате в приложение.
@Injectable({ providedIn: 'root' })
export class PwaUpdate {
  private readonly updates = inject(SwUpdate);

  constructor() {
    if (!this.updates.isEnabled) return; // SW включён только в проде по HTTPS

    this.updates.versionUpdates
      .pipe(filter((e): e is VersionReadyEvent => e.type === 'VERSION_READY'))
      .subscribe(() => {
        void this.updates.activateUpdate().then(() => document.location.reload());
      });

    void this.check();
    // Возврат в приложение (открыли PWA снова) — проверить, нет ли обновления.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void this.check();
    });
  }

  private check(): Promise<unknown> {
    return this.updates.checkForUpdate().catch(() => false);
  }
}
