import { Injectable, inject, signal } from '@angular/core';
import { EasyWeekApi } from '../services/api';

// Один плеер озвучки на приложение: играет один шаг за раз, второй тап по той же кнопке —
// стоп, тап по другой — переключение. Источник — GET /api/tts?text=… (OpenRouter Fish Audio;
// кэш на бэке и в браузере по URL); `play()` зовём синхронно в обработчике тапа — иначе iOS
// блокирует звук.
// Прогрев: первый тап в группе шагов (рецепт / план готовки) отправляет остальные шаги в
// POST /api/tts/warm — бэк озвучивает их фоном, и следующие тапы играют сразу. Порядок —
// от нажатого шага дальше по кругу (следующий шаг нужен раньше всех). Группа греется один раз
// за сессию (ключ — все тексты); бэк уже закэшированное пропускает.
export type TtsState = 'idle' | 'loading' | 'playing';

/** Ключ шага: схлопываем пробелы — так же нормализует бэк (один кэш на один текст). */
export function ttsKey(text: string): string {
  return text.trim().replace(/\s+/g, ' ');
}

@Injectable({ providedIn: 'root' })
export class TtsPlayer {
  private readonly api = inject(EasyWeekApi);
  private audio: HTMLAudioElement | null = null;
  private readonly warmed = new Set<string>();

  readonly current = signal(''); // ключ шага, который грузится/играет
  readonly state = signal<TtsState>('idle');
  readonly error = signal(''); // текст последней ошибки (для title кнопки)

  /** Тап по кнопке шага. group — все шаги этого рецепта/плана (для фонового прогрева). */
  toggle(text: string, group: readonly string[] = []): void {
    const key = ttsKey(text);
    if (!key) return;
    if (this.current() === key && this.state() !== 'idle') {
      this.stop();
      return;
    }
    this.warm(key, group);
    const a = this.ensure();
    a.pause();
    a.src = `/api/tts?text=${encodeURIComponent(key)}`;
    this.current.set(key);
    this.state.set('loading');
    this.error.set('');
    // Ошибка загрузки/квоты прилетит событием error; NotAllowedError (не из жеста) — сюда.
    a.play().catch(() => this.fail('Не удалось воспроизвести'));
  }

  stop(): void {
    this.audio?.pause();
    this.state.set('idle');
    this.current.set('');
  }

  private warm(key: string, group: readonly string[]): void {
    const keys = group.map(ttsKey).filter(Boolean);
    if (keys.length < 2) return;
    const gid = keys.join('\n');
    if (this.warmed.has(gid)) return;
    this.warmed.add(gid);
    const i = Math.max(0, keys.indexOf(key));
    const rest = [...keys.slice(i + 1), ...keys.slice(0, i)].filter((k) => k !== key);
    if (!rest.length) return;
    this.api.ttsWarm(rest).subscribe({
      error: () => this.warmed.delete(gid), // не вышло — попробуем при следующем тапе
    });
  }

  private ensure(): HTMLAudioElement {
    if (this.audio) return this.audio;
    const a = new Audio();
    a.preload = 'auto';
    a.addEventListener('playing', () => this.state.set('playing'));
    a.addEventListener('ended', () => this.stop());
    a.addEventListener('error', () => this.fail('Не удалось озвучить — попробуйте ещё раз'));
    this.audio = a;
    return a;
  }

  private fail(msg: string): void {
    this.error.set(msg);
    this.stop();
  }
}
