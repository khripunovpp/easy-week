import { Component, computed, inject } from '@angular/core';
import { TtsPlayer } from './tts-player';

/** «1:07» из секунд; пусто, если длительность неизвестна. */
export function formatClock(sec: number): string {
  if (!Number.isFinite(sec)) return '';
  const t = Math.max(0, Math.round(sec));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

// Нижняя панель озвучки (GUIDEBOOK → «Озвучка шага»): видна, пока есть текущий шаг (играет,
// на паузе, грузится или ошибка). Живёт в оболочке приложения над таб-баром — звук и управление
// не пропадают при уходе со страницы рецепта. Дублирует ▶/❚❚ кнопки шага, даёт дорожку с
// перемоткой и режим: «Один шаг» / «Подряд» (по окончании — следующий шаг: готов — сразу,
// нет — грузим). × — стоп и скрыть.
@Component({
  selector: 'ew-tts-panel',
  template: `
    <section class="ttsp" aria-label="Озвучка шага">
      <div class="ttsp__row">
        <button
          type="button"
          class="ttsp__play"
          [class.ttsp__play--busy]="p.state() === 'loading'"
          [attr.aria-label]="p.state() === 'playing' ? 'Пауза' : 'Играть'"
          (click)="p.togglePlay()">
          @if (p.state() === 'playing') {
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <rect x="7" y="5.5" width="3.4" height="13" rx="1.2" />
              <rect x="13.6" y="5.5" width="3.4" height="13" rx="1.2" />
            </svg>
          } @else {
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M8.5 6.3v11.4a.9.9 0 0 0 1.4.8l9-5.7a.9.9 0 0 0 0-1.6l-9-5.7a.9.9 0 0 0-1.4.8z" />
            </svg>
          }
        </button>

        <div class="ttsp__info">
          <span class="ttsp__title">{{ title() }}</span>
          <span class="ttsp__text" [class.ttsp__text--err]="p.state() === 'error'">
            {{ p.state() === 'error' ? p.error() : p.current() }}
          </span>
        </div>

        @if (p.group().length > 1) {
          <button
            type="button"
            class="ttsp__mode"
            [class.ttsp__mode--on]="p.mode() === 'all'"
            [attr.aria-pressed]="p.mode() === 'all'"
            [attr.title]="p.mode() === 'all' ? 'После шага — следующий' : 'Только этот шаг'"
            (click)="p.setMode(p.mode() === 'all' ? 'one' : 'all')">
            @if (p.mode() === 'all') {
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M4 7h11M4 12h11M4 17h7" stroke-linecap="round" />
                <path d="M17 14l3 3-3 3" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              Подряд
            } @else {
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M9 7h2v10" stroke-linecap="round" stroke-linejoin="round" />
                <path d="M8.5 17h5" stroke-linecap="round" />
              </svg>
              Один шаг
            }
          </button>
        }

        <button type="button" class="ttsp__close" aria-label="Закрыть плеер" (click)="p.stop()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M7 7l10 10M17 7L7 17" stroke-linecap="round" />
          </svg>
        </button>
      </div>

      <div class="ttsp__track">
        <span class="ttsp__time">{{ elapsed() }}</span>
        <input
          class="ttsp__range"
          type="range"
          min="0"
          max="1000"
          step="1"
          aria-label="Перемотка"
          [style.--p]="p.progress() * 100 + '%'"
          [value]="p.progress() * 1000"
          [disabled]="!canSeek()"
          (input)="p.seek(+$any($event.target).value / 1000)" />
        <span class="ttsp__time ttsp__time--end">{{ remaining() }}</span>
      </div>
    </section>
  `,
  styles: `
    :host {
      position: fixed;
      left: 50%;
      bottom: calc(var(--tabbar-h) + var(--safe-bottom));
      transform: translateX(-50%);
      width: 100%;
      max-width: 430px;
      padding: 0 var(--page-pad-x) 8px;
      z-index: 19;
      animation: ttsp-in 0.18s ease;
    }
    @keyframes ttsp-in {
      from {
        opacity: 0;
        transform: translate(-50%, 8px);
      }
    }
    .ttsp {
      height: var(--tts-panel-h);
      padding: 8px 10px 6px;
      border-radius: var(--r-md);
      background: var(--surface);
      box-shadow: var(--shadow-float);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .ttsp__row {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .ttsp__play {
      flex-shrink: 0;
      display: grid;
      place-items: center;
      width: 38px;
      height: 38px;
      border-radius: 50%;
      background: var(--accent);
      color: #fff;
      transition: transform 0.12s ease;
    }
    .ttsp__play:active {
      transform: scale(0.92);
    }
    .ttsp__play svg {
      width: 20px;
      height: 20px;
    }
    .ttsp__play--busy {
      animation: ttsp-pulse 1s ease-in-out infinite;
    }
    .ttsp__info {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 1px;
    }
    .ttsp__title {
      font-size: 13px;
      font-weight: 700;
      color: var(--ink);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ttsp__text {
      font-size: 12px;
      color: var(--ink-2);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ttsp__text--err {
      color: var(--no);
    }
    .ttsp__mode {
      flex-shrink: 0;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      height: 28px;
      padding: 0 10px 0 8px;
      border-radius: var(--r-pill);
      background: var(--surface-sunk);
      color: var(--ink-2);
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }
    .ttsp__mode svg {
      width: 15px;
      height: 15px;
    }
    .ttsp__mode--on {
      background: var(--accent-soft);
      color: var(--accent);
    }
    .ttsp__close {
      flex-shrink: 0;
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      color: var(--ink-3);
    }
    .ttsp__close:active {
      background: var(--surface-sunk);
    }
    .ttsp__close svg {
      width: 18px;
      height: 18px;
    }
    .ttsp__track {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ttsp__time {
      flex-shrink: 0;
      min-width: 30px;
      font-size: 11px;
      font-weight: 600;
      color: var(--ink-3);
      font-variant-numeric: tabular-nums;
    }
    .ttsp__time--end {
      text-align: right;
    }
    /* Дорожка: заливка пройденного --accent через --p, бегунок 14px */
    .ttsp__range {
      flex: 1;
      min-width: 0;
      height: 18px;
      margin: 0;
      background: transparent;
      -webkit-appearance: none;
      appearance: none;
      cursor: pointer;
    }
    .ttsp__range:disabled {
      cursor: default;
      opacity: 0.6;
    }
    .ttsp__range::-webkit-slider-runnable-track {
      height: 4px;
      border-radius: 2px;
      background: linear-gradient(to right, var(--accent) var(--p, 0%), var(--line) var(--p, 0%));
    }
    .ttsp__range::-moz-range-track {
      height: 4px;
      border-radius: 2px;
      background: linear-gradient(to right, var(--accent) var(--p, 0%), var(--line) var(--p, 0%));
    }
    .ttsp__range::-webkit-slider-thumb {
      -webkit-appearance: none;
      width: 14px;
      height: 14px;
      margin-top: -5px;
      border-radius: 50%;
      background: var(--accent);
      border: 2px solid var(--surface);
      box-shadow: var(--shadow-soft);
    }
    .ttsp__range::-moz-range-thumb {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--accent);
      border: 2px solid var(--surface);
    }
    @keyframes ttsp-pulse {
      50% {
        opacity: 0.45;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      :host {
        animation: none;
      }
      .ttsp__play--busy {
        animation: none;
        opacity: 0.6;
      }
    }
  `,
})
export class TtsPanel {
  readonly p = inject(TtsPlayer);

  readonly title = computed(() => {
    const n = this.p.group().length;
    const i = this.p.index();
    const step = n > 1 && i >= 0 ? `Шаг ${i + 1} из ${n}` : 'Шаг';
    return this.p.source() ? `${step} · ${this.p.source()}` : step;
  });
  readonly canSeek = computed(() => {
    const d = this.p.duration();
    return Number.isFinite(d) && d > 0 && this.p.state() !== 'loading';
  });
  readonly elapsed = computed(() => formatClock(this.p.elapsed()) || '0:00');
  readonly remaining = computed(() => {
    const r = formatClock(this.p.remaining());
    return r ? `−${r}` : '';
  });
}
