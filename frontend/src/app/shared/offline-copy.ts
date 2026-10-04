import { HttpErrorResponse } from '@angular/common/http';
import { Observable, catchError, of, tap, throwError } from 'rxjs';

// Офлайн-копия ответов, которые service worker не кэширует: рецепт блюда и план готовки
// открываются POST-ом (open генерит при первом открытии), а Angular SW кэширует только GET.
// Каждый удачный ответ кладём в localStorage (последние MAX штук), без сети (status 0 —
// запрос не дошёл до сервера) отдаём копию. Только для чтения «открыть»: выбор новой модели
// или ↻ без сети — честная ошибка. Хранилище может быть недоступно (приватный режим) — молча мимо.
const PREFIX = 'ew-offline:';
const INDEX = 'ew-offline-index';
const MAX = 80;

function readIndex(): string[] {
  try {
    const raw = localStorage.getItem(INDEX);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function saveOfflineCopy(key: string, value: unknown): void {
  try {
    const index = readIndex().filter((k) => k !== key);
    index.unshift(key);
    for (const old of index.splice(MAX)) localStorage.removeItem(PREFIX + old);
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
    localStorage.setItem(INDEX, JSON.stringify(index));
  } catch {
    // переполнено/запрещено — офлайн-копии просто не будет
  }
}

export function readOfflineCopy<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

/** Удачный ответ — в копию; нет сети и fallback=true — копия вместо ошибки. */
export function withOfflineCopy<T>(key: string, src: Observable<T>, fallback: boolean): Observable<T> {
  return src.pipe(
    tap((v) => saveOfflineCopy(key, v)),
    catchError((err: unknown) => {
      const offline = err instanceof HttpErrorResponse && err.status === 0;
      const copy = offline && fallback ? readOfflineCopy<T>(key) : null;
      return copy ? of(copy) : throwError(() => err);
    }),
  );
}
