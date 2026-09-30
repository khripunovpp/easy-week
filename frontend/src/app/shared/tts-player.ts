import { Injectable, inject, signal } from '@angular/core';
import { EasyWeekApi } from '../services/api';

// Один плеер озвучки на приложение: играет один шаг за раз, второй тап по той же кнопке —
// стоп, тап по другой — переключение. Источник — GET /api/tts?text=… (OpenRouter Fish Audio,
// кэш на бэке); `play()` зовём синхронно в обработчике тапа — иначе iOS блокирует звук.
//
// Прогрев: первый тап в группе шагов (рецепт / план готовки) ставит остальные шаги в очередь —
// тянем их тем же GET по WARM_PARALLEL штук (бесплатная модель, не душим) в порядке от нажатого
// по кругу и держим аудио в памяти (blob URL). Пока шаг качается, он в `warming` — его кнопка
// пульсирует; готовый шаг стартует без сети. Тап по шагу, который как раз качается, ставит
// <audio src> на обычный URL — бэк склеит запрос с идущей генерацией.
//
// Прогресс проигрывания — `progress` (0..1) и `remaining` (сек) из timeupdate.
export type TtsState = 'idle' | 'loading' | 'playing';

const WARM_PARALLEL = 2;
const READY_MAX = 80; // сколько готовых блобов держим (≈ несколько рецептов), старые освобождаем

/** Ключ шага: схлопываем пробелы — так же нормализует бэк (один кэш на один текст). */
export function ttsKey(text: string): string {
  return text.trim().replace(/\s+/g, ' ');
}

@Injectable({ providedIn: 'root' })
export class TtsPlayer {
  private readonly api = inject(EasyWeekApi);
  private audio: HTMLAudioElement | null = null;

  readonly current = signal(''); // ключ шага, который грузится/играет
  readonly state = signal<TtsState>('idle');
  readonly error = signal(''); // текст последней ошибки (для title кнопки)
  readonly progress = signal(0); // доля проигранного, 0..1
  readonly remaining = signal(NaN); // осталось секунд (NaN — длительность ещё неизвестна)
  readonly warming = signal<ReadonlySet<string>>(new Set()); // шаги, которые качаются фоном

  private readonly ready = new Map<string, string>(); // ключ → blob URL готового аудио
  private queue: string[] = [];
  private inflight = 0;

  /** Тап по кнопке шага. group — все шаги этого рецепта/плана (для фонового прогрева). */
  toggle(text: string, group: readonly string[] = []): void {
    const key = ttsKey(text);
    if (!key) return;
    if (this.current() === key && this.state() !== 'idle') {
      this.stop();
      return;
    }
    const a = this.ensure();
    a.pause();
    a.src = this.ready.get(key) ?? this.api.ttsUrl(key);
    this.current.set(key);
    this.state.set('loading');
    this.error.set('');
    this.progress.set(0);
    this.remaining.set(NaN);
    // Ошибка загрузки/квоты прилетит событием error; NotAllowedError (не из жеста) — сюда.
    a.play().catch(() => this.fail('Не удалось воспроизвести'));
    this.warm(key, group);
  }

  stop(): void {
    this.audio?.pause();
    this.state.set('idle');
    this.current.set('');
    this.progress.set(0);
    this.remaining.set(NaN);
  }

  isReady(text: string): boolean {
    return this.ready.has(ttsKey(text));
  }

  // ---- прогрев ----

  private warm(key: string, group: readonly string[]): void {
    const keys = group.map(ttsKey).filter(Boolean);
    const i = keys.indexOf(key);
    // От нажатого дальше по кругу: следующий шаг понадобится раньше всех.
    const order = i >= 0 ? [...keys.slice(i + 1), ...keys.slice(0, i)] : keys;
    const warming = this.warming();
    for (const k of order) {
      if (k === key || this.ready.has(k) || warming.has(k) || this.queue.includes(k)) continue;
      this.queue.push(k);
    }
    this.pump();
  }

  private pump(): void {
    while (this.inflight < WARM_PARALLEL && this.queue.length) {
      const k = this.queue.shift()!;
      this.inflight++;
      this.setWarming(k, true);
      this.api.ttsAudio(k).subscribe({
        next: (blob) => this.store(k, blob),
        error: () => {
          /* не вышло — тап по шагу повторит обычным GET */
        },
        complete: () => this.done(k),
      });
    }
  }

  private done(k: string): void {
    this.inflight--;
    this.setWarming(k, false);
    this.pump();
  }

  private store(k: string, blob: Blob): void {
    if (!blob.size) return;
    if (this.ready.size >= READY_MAX) {
      const oldest = this.ready.keys().next().value;
      if (oldest !== undefined) {
        URL.revokeObjectURL(this.ready.get(oldest)!);
        this.ready.delete(oldest);
      }
    }
    this.ready.set(k, URL.createObjectURL(blob));
  }

  private setWarming(k: string, on: boolean): void {
    const next = new Set(this.warming());
    if (on) next.add(k);
    else next.delete(k);
    this.warming.set(next);
  }

  // ---- аудио-элемент ----

  private ensure(): HTMLAudioElement {
    if (this.audio) return this.audio;
    const a = new Audio();
    a.preload = 'auto';
    a.addEventListener('playing', () => this.state.set('playing'));
    a.addEventListener('timeupdate', () => this.tick(a));
    a.addEventListener('durationchange', () => this.tick(a));
    a.addEventListener('ended', () => this.stop());
    a.addEventListener('error', () => this.fail('Не удалось озвучить — попробуйте ещё раз'));
    this.audio = a;
    return a;
  }

  private tick(a: HTMLAudioElement): void {
    const d = a.duration;
    if (!Number.isFinite(d) || d <= 0) return;
    this.progress.set(Math.min(1, a.currentTime / d));
    this.remaining.set(Math.max(0, d - a.currentTime));
  }

  private fail(msg: string): void {
    this.error.set(msg);
    this.stop();
  }
}
