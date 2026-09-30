import { Injectable, signal } from '@angular/core';

// Один плеер озвучки на приложение: играет один шаг за раз, второй тап по той же кнопке —
// стоп, тап по другой — переключение. Источник — GET /api/tts?text=… (OpenRouter Fish Audio;
// кэш на бэке и в браузере по URL); `play()` зовём синхронно в обработчике тапа — иначе iOS
// блокирует звук.
export type TtsState = 'idle' | 'loading' | 'playing';

/** Ключ шага: схлопываем пробелы — так же нормализует бэк (один кэш на один текст). */
export function ttsKey(text: string): string {
  return text.trim().replace(/\s+/g, ' ');
}

@Injectable({ providedIn: 'root' })
export class TtsPlayer {
  private audio: HTMLAudioElement | null = null;

  readonly current = signal(''); // ключ шага, который грузится/играет
  readonly state = signal<TtsState>('idle');
  readonly error = signal(''); // текст последней ошибки (для title кнопки)

  toggle(text: string): void {
    const key = ttsKey(text);
    if (!key) return;
    if (this.current() === key && this.state() !== 'idle') {
      this.stop();
      return;
    }
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
