import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';
import { AuthService } from './auth';

/**
 * Гард приложения: на старте (один раз, дальше из кэша) спрашивает /api/auth/status.
 * Не вошли → /login?returnUrl=…. Сервер недоступен (офлайн) → пускаем: PWA работает
 * на закэшированных данных, а живой запрос при появлении сети сам получит 401.
 */
export const authGuard: CanActivateFn = async (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const s = await auth.ensureStatus();
  if (!s || !s.required || s.authenticated) return true;
  return router.createUrlTree(['/login'], { queryParams: { returnUrl: state.url } });
};

/** /login: уже вошли (или пароль не нужен) — сразу на главную. */
export const loginGuard: CanActivateFn = async () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const s = await auth.ensureStatus();
  return s && (!s.required || s.authenticated) ? router.createUrlTree(['/home']) : true;
};

/** Любой 401 от /api (кроме /api/auth/*) → на экран входа. Ошибку пробрасываем дальше. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  return next(req).pipe(
    catchError((err: unknown) => {
      if (
        err instanceof HttpErrorResponse &&
        err.status === 401 &&
        req.url.includes('/api/') &&
        !req.url.includes('/api/auth/')
      ) {
        auth.handleUnauthorized();
      }
      return throwError(() => err);
    }),
  );
};
