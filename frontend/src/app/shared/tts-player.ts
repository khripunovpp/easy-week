import { HttpErrorResponse } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { EasyWeekApi } from '../services/api';

// Один плеер озвучки на приложение (GUIDEBOOK → «Озвучка шага»). Источник — GET /api/tts?text=…
// (OpenRouter Fish Audio, кэш на бэке); `play()` зовём синхронно в обработчике тапа — иначе iOS
// блокирует звук. Дальше тот же <audio> можно переключать и из события ended (режим «Подряд»).
//
// Группа — все шаги экрана (рецепт / план готовки). Первый тап ставит остальные шаги в очередь
// прогрева: тянем их тем же GET по WARM_PARALLEL (бесплатная модель, не душим) от нажатого по
// кругу и держим в памяти (blob URL). Пока шаг качается — `warming` (кнопка пульсирует), готов —
// `readyKeys` (кнопка оранжевая), стартует без сети.
//
// Режим: 'one' — играем один шаг; 'all' — по окончании включаем следующий шаг группы (готов —
// сразу, нет — грузим). Выбор режима — на устройстве (localStorage), это удобство, не данные.
//
// Лимиты (дневной лимит озвучки на бэке, лимит бесплатных моделей OpenRouter): <audio> текста
// ошибки не видит, поэтому при сбое спрашиваем GET /api/tts/status и показываем причину в
// панели. Прогрев на 429 останавливается и до сброса лимита не запускается (уже озвученные
// шаги при этом играют — они из кэша).
export type TtsState = 'idle' | 'loading' | 'playing' | 'paused' | 'error';
export type TtsMode = 'one' | 'all';

const WARM_PARALLEL = 2;
const READY_MAX = 80; // сколько готовых блобов держим (≈ несколько рецептов), старые освобождаем
const MODE_KEY = 'ew.ttsMode';

/** Ключ шага: схлопываем пробелы — так же нормализует бэк (один кэш на один текст). */
export function ttsKey(text: string): string {
  return text.trim().replace(/\s+/g, ' ');
}

@Injectable({ providedIn: 'root' })
export class TtsPlayer {
  private readonly api = inject(EasyWeekApi);
  private audio: HTMLAudioElement | null = null;

  readonly current = signal(''); // ключ текущего шага
  readonly state = signal<TtsState>('idle');
  readonly error = signal('');
  readonly elapsed = signal(0); // сек
  readonly duration = signal(NaN); // сек (NaN — ещё неизвестна)
  readonly progress = computed(() => {
    const d = this.duration();
    return Number.isFinite(d) && d > 0 ? Math.min(1, this.elapsed() / d) : 0;
  });
  readonly remaining = computed(() => {
    const d = this.duration();
    return Number.isFinite(d) ? Math.max(0, d - this.elapsed()) : NaN;
  });
  readonly warming = signal<ReadonlySet<string>>(new Set()); // качаются фоном
  readonly readyKeys = signal<ReadonlySet<string>>(new Set()); // готовы (в памяти или уже играли)
  readonly mode = signal<TtsMode>(this.readMode());

  // Текущая группа (для «Подряд» и подписи «Шаг N из M»).
  readonly group = signal<readonly string[]>([]);
  readonly source = signal(''); // откуда шаги: название блюда / «План готовки»
  readonly index = computed(() => this.group().indexOf(this.current()));
  readonly visible = computed(() => this.state() !== 'idle');

  private readonly blobs = new Map<string, string>(); // ключ → blob URL
  private queue: string[] = [];
  private inflight = 0;
  private limitedUntil = 0; // ms: лимит новых генераций — прогрев не запускаем

  /** Тап по кнопке шага: другой шаг — играть его; свой — пауза/продолжить/повтор. */
  toggle(text: string, group: readonly string[] = [], source = ''): void {
    const key = ttsKey(text);
    if (!key) return;
    if (this.current() === key) {
      const st = this.state();
      if (st === 'playing') return this.pause();
      if (st === 'paused') return this.resume();
      if (st === 'loading') return this.stop();
    }
    const keys = group.map(ttsKey).filter(Boolean);
    this.group.set(keys.includes(key) ? keys : [key]);
    this.source.set(source);
    this.start(key);
    this.warm(key, keys);
  }

  /** Кнопка ▶/❚❚ панели. */
  togglePlay(): void {
    const st = this.state();
    if (st === 'playing') this.pause();
    else if (st === 'paused') this.resume();
    else if (st === 'error' && this.current()) this.start(this.current());
    else if (st === 'loading') this.stop();
  }

  pause(): void {
    this.audio?.pause();
    if (this.state() === 'playing') this.state.set('paused');
  }

  resume(): void {
    const a = this.audio;
    if (!a) return;
    this.state.set('playing');
    a.play().catch(() => this.fail('Не удалось воспроизвести'));
  }

  /** Закрыть: стоп и скрыть панель. */
  stop(): void {
    this.audio?.pause();
    this.state.set('idle');
    this.current.set('');
    this.elapsed.set(0);
    this.duration.set(NaN);
    this.error.set('');
    this.setMedia(null);
  }

  seek(fraction: number): void {
    const a = this.audio;
    const d = this.duration();
    if (!a || !Number.isFinite(d) || d <= 0) return;
    a.currentTime = Math.max(0, Math.min(1, fraction)) * d;
    this.elapsed.set(a.currentTime);
  }

  setMode(m: TtsMode): void {
    this.mode.set(m);
    try {
      localStorage.setItem(MODE_KEY, m);
    } catch {
      /* приватный режим — просто не запомним */
    }
  }

  /** Следующий шаг группы (режим «Подряд», кнопка «дальше» на экране блокировки). */
  next(): boolean {
    const g = this.group();
    const i = this.index();
    if (i < 0 || i + 1 >= g.length) return false;
    this.start(g[i + 1]);
    return true;
  }

  prev(): boolean {
    const i = this.index();
    if (i <= 0) return false;
    this.start(this.group()[i - 1]);
    return true;
  }

  // ---- проигрывание ----

  private start(key: string): void {
    const a = this.ensure();
    a.pause();
    a.src = this.blobs.get(key) ?? this.api.ttsUrl(key);
    this.current.set(key);
    this.state.set('loading');
    this.error.set('');
    this.elapsed.set(0);
    this.duration.set(NaN);
    this.setMedia(key);
    // Ошибка загрузки/квоты прилетит событием error; NotAllowedError (не из жеста) — сюда.
    a.play().catch((e: unknown) => {
      if ((e as DOMException)?.name !== 'AbortError') this.fail('Не удалось воспроизвести');
    });
  }

  private ensure(): HTMLAudioElement {
    if (this.audio) return this.audio;
    const a = new Audio();
    a.preload = 'auto';
    a.addEventListener('playing', () => {
      this.state.set('playing');
      this.markReady(this.current()); // сыграл — на бэке в кэше, повтор мгновенный
    });
    a.addEventListener('pause', () => {
      // Внешняя пауза (система, наушники, экран блокировки); конец трека обрабатывает ended.
      if (!a.ended && this.state() === 'playing') this.state.set('paused');
    });
    a.addEventListener('timeupdate', () => this.elapsed.set(a.currentTime));
    a.addEventListener('durationchange', () => {
      if (Number.isFinite(a.duration)) this.duration.set(a.duration);
    });
    a.addEventListener('ended', () => {
      if (this.mode() === 'all' && this.next()) return;
      this.stop();
    });
    a.addEventListener('error', () => {
      if (this.state() === 'loading' || this.state() === 'playing') this.explainFailure();
    });
    this.audio = a;
    this.bindMediaSession();
    return a;
  }

  private fail(msg: string): void {
    this.audio?.pause();
    this.error.set(msg);
    this.state.set('error');
  }

  /** Сбой загрузки аудио: причину (лимит) узнаём у бэка, иначе — общий текст. */
  private explainFailure(): void {
    const key = this.current();
    const generic = 'Не удалось озвучить — нажмите ▶, чтобы повторить';
    this.fail(generic);
    this.api.ttsStatus().subscribe({
      next: (st) => {
        if (st.available || this.current() !== key) return;
        this.noteLimit(st.resetAt);
        this.fail(st.detail || generic);
      },
    });
  }

  private noteLimit(resetAt: string | null): void {
    const t = resetAt ? Date.parse(resetAt) : NaN;
    this.limitedUntil = Number.isFinite(t) ? t : Date.now() + 60_000;
    this.queue = []; // прогрев бессмыслен до сброса лимита
  }

  // ---- прогрев ----

  private warm(key: string, keys: readonly string[]): void {
    if (Date.now() < this.limitedUntil) return; // лимит: новые шаги не греем
    const i = keys.indexOf(key);
    // От нажатого дальше по кругу: следующий шаг понадобится раньше всех.
    const order = i >= 0 ? [...keys.slice(i + 1), ...keys.slice(0, i)] : keys;
    const warming = this.warming();
    const ready = this.readyKeys();
    for (const k of order) {
      if (k === key || ready.has(k) || warming.has(k) || this.queue.includes(k)) continue;
      this.queue.push(k);
    }
    this.pump();
  }

  private pump(): void {
    while (this.inflight < WARM_PARALLEL && this.queue.length) {
      const k = this.queue.shift()!;
      this.inflight++;
      this.setIn(this.warming, k, true);
      this.api.ttsAudio(k).subscribe({
        next: (blob) => this.store(k, blob),
        error: (e: unknown) => {
          // 429 — лимит: останавливаем прогрев до сброса (время — из /api/tts/status).
          if (e instanceof HttpErrorResponse && e.status === 429) {
            this.queue = [];
            this.limitedUntil = Date.now() + 60_000;
            this.api.ttsStatus().subscribe({ next: (st) => !st.available && this.noteLimit(st.resetAt) });
          }
          this.done(k); // прочие сбои — тап по шагу повторит обычным GET
        },
        complete: () => this.done(k),
      });
    }
  }

  private done(k: string): void {
    this.inflight--;
    this.setIn(this.warming, k, false);
    this.pump();
  }

  private store(k: string, blob: Blob): void {
    if (!blob.size) return;
    if (this.blobs.size >= READY_MAX) {
      const oldest = this.blobs.keys().next().value;
      if (oldest !== undefined && oldest !== this.current()) {
        URL.revokeObjectURL(this.blobs.get(oldest)!);
        this.blobs.delete(oldest);
        this.setIn(this.readyKeys, oldest, false);
      }
    }
    this.blobs.set(k, URL.createObjectURL(blob));
    this.markReady(k);
  }

  private markReady(k: string): void {
    if (k && !this.readyKeys().has(k)) this.setIn(this.readyKeys, k, true);
  }

  private setIn(sig: ReturnType<typeof signal<ReadonlySet<string>>>, k: string, on: boolean): void {
    const next = new Set(sig());
    if (on) next.add(k);
    else next.delete(k);
    sig.set(next);
  }

  // ---- экран блокировки / наушники (Media Session) ----

  private bindMediaSession(): void {
    const ms = typeof navigator !== 'undefined' ? navigator.mediaSession : undefined;
    if (!ms) return;
    const on = (action: MediaSessionAction, fn: () => void) => {
      try {
        ms.setActionHandler(action, fn);
      } catch {
        /* действие не поддерживается браузером */
      }
    };
    on('play', () => this.resume());
    on('pause', () => this.pause());
    on('stop', () => this.stop());
    on('nexttrack', () => void this.next());
    on('previoustrack', () => void this.prev());
  }

  private setMedia(key: string | null): void {
    const ms = typeof navigator !== 'undefined' ? navigator.mediaSession : undefined;
    if (!ms || typeof MediaMetadata === 'undefined') return;
    if (!key) {
      ms.metadata = null;
      return;
    }
    const g = this.group();
    const i = g.indexOf(key);
    ms.metadata = new MediaMetadata({
      title: i >= 0 && g.length > 1 ? `Шаг ${i + 1} из ${g.length}` : 'Шаг',
      artist: this.source() || 'Easy Week',
      album: 'Easy Week',
    });
  }

  private readMode(): TtsMode {
    try {
      return localStorage.getItem(MODE_KEY) === 'all' ? 'all' : 'one';
    } catch {
      return 'one';
    }
  }
}
