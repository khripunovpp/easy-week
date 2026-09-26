import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';

// Вход по общему паролю. Сессия — HttpOnly-кука `ew_session` (JS её не видит),
// поэтому состояние узнаём у бэкенда через /api/auth/status и кэшируем.
export interface AuthStatus {
  authenticated: boolean;
  required: boolean;
}

const AUTH_BASE = '/api/auth';

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);

  // null — ещё не спрашивали. Кэш на всю сессию вкладки (сбрасывается при 401/логауте).
  readonly status = signal<AuthStatus | null>(null);
  private pending: Promise<AuthStatus | null> | null = null;

  /** Статус из кэша или один запрос к бэку. null — сервер недоступен (офлайн). */
  ensureStatus(): Promise<AuthStatus | null> {
    const cached = this.status();
    if (cached) return Promise.resolve(cached);
    this.pending ??= firstValueFrom(this.http.get<AuthStatus>(`${AUTH_BASE}/status`))
      .then((s) => {
        this.status.set(s);
        return s;
      })
      // Офлайн/сеть упала — не блокируем: данные из кэша SW всё равно доступны,
      // а живые запросы при появлении сети сами получат 401 → логин.
      .catch(() => null)
      .finally(() => (this.pending = null));
    return this.pending;
  }

  /** Вход. Возвращает текст ошибки или null при успехе. */
  async login(password: string): Promise<string | null> {
    try {
      const s = await firstValueFrom(
        this.http.post<AuthStatus>(`${AUTH_BASE}/login`, { password }),
      );
      this.status.set(s);
      return null;
    } catch (e) {
      const status = e instanceof HttpErrorResponse ? e.status : 0;
      if (status === 401) return 'Неверный пароль';
      if (status === 429) return 'Слишком много попыток — подождите минуту';
      if (status === 0 || status === 504) return 'Нет связи с сервером';
      return 'Не получилось войти, попробуйте ещё раз';
    }
  }

  async logout(): Promise<void> {
    try {
      await firstValueFrom(this.http.post(`${AUTH_BASE}/logout`, {}));
    } catch {
      // даже если сервер не ответил — локально считаем, что вышли
    }
    await this.clearApiCache();
    this.status.set({ authenticated: false, required: true });
    await this.router.navigateByUrl('/login');
  }

  /**
   * Бэк ответил 401 (сессия протухла / сменили пароль) — сбрасываем кэш статуса и ведём
   * на /login, запомнив, куда вернуться. Зовут интерсептор и fetch-SSE из api.ts.
   */
  handleUnauthorized(): void {
    const cur = this.status();
    this.status.set({ authenticated: false, required: cur?.required ?? true });
    const url = this.router.url;
    if (url.startsWith('/login')) return;
    void this.router.navigate(['/login'], { queryParams: { returnUrl: url } });
  }

  /** После выхода чистим закэшированные service worker'ом ответы API (планы/блюда). */
  private async clearApiCache(): Promise<void> {
    if (typeof caches === 'undefined') return;
    try {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k.includes('api-plans')).map((k) => caches.delete(k)),
      );
    } catch {
      // Cache API недоступен (http/приватный режим) — не критично
    }
  }
}
