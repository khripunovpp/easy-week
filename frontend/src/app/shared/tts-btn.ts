import { Component, computed, inject, input } from '@angular/core';
import { TtsPlayer, ttsKey } from './tts-player';

// Кнопка озвучки шага 🔊 (GUIDEBOOK → «Озвучка шага»): круглая 28px на --surface-sunk.
// Состояния: грузится (свой шаг или качается фоном в прогреве) — пульсирует; играет —
// коралловая заливка, значок «стоп», по краю белое кольцо прогресса (пройдено), под кнопкой
// остаток времени. Состояние общее (TtsPlayer): играет один шаг, тап по другому переключает.
// [group] — все шаги рецепта/плана: первый тап греет остальные фоном (см. TtsPlayer.warm).
const R = 12; // радиус кольца в viewBox 28×28 (внутри края кнопки)
const CIRC = 2 * Math.PI * R;

@Component({
  selector: 'ew-tts-btn',
  template: `
    <button
      type="button"
      class="tts"
      [class.tts--on]="playing()"
      [class.tts--busy]="loading() || warming()"
      [attr.aria-label]="playing() ? 'Остановить озвучку' : 'Озвучить шаг'"
      [attr.aria-pressed]="playing() || loading()"
      [attr.title]="active() && player.error() ? player.error() : null"
      (click)="player.toggle(text(), group())">
      @if (playing()) {
        <svg class="tts__ring" viewBox="0 0 28 28" aria-hidden="true">
          <circle class="tts__ring-track" cx="14" cy="14" [attr.r]="r" />
          <circle
            class="tts__ring-fill"
            cx="14"
            cy="14"
            [attr.r]="r"
            [attr.stroke-dasharray]="circ"
            [attr.stroke-dashoffset]="dashOffset()" />
        </svg>
        <svg class="tts__ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <rect x="7" y="7" width="10" height="10" rx="2" />
        </svg>
      } @else {
        <svg class="tts__ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true">
          <path d="M4 9.5v5a1 1 0 0 0 1 1h2.6l4.2 3.3a.6.6 0 0 0 1-.5V5.7a.6.6 0 0 0-1-.5L7.6 8.5H5a1 1 0 0 0-1 1z" stroke-linejoin="round" />
          <path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" stroke-linecap="round" />
        </svg>
      }
    </button>
    @if (playing() && remainingLabel()) {
      <span class="tts__time" aria-live="off">−{{ remainingLabel() }}</span>
    }
  `,
  styles: `
    :host {
      position: relative;
      display: inline-flex;
      flex-shrink: 0;
    }
    .tts {
      position: relative;
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background: var(--surface-sunk);
      color: var(--ink-2);
      transition:
        transform 0.12s ease,
        background 0.15s ease,
        color 0.15s ease;
    }
    .tts:active {
      transform: scale(0.92);
    }
    .tts--on {
      background: var(--accent);
      color: #fff;
    }
    .tts--busy {
      animation: tts-pulse 1s ease-in-out infinite;
    }
    .tts__ic {
      width: 16px;
      height: 16px;
    }
    /* Кольцо прогресса — по краю кнопки, растёт от 12 часов по часовой */
    .tts__ring {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      transform: rotate(-90deg);
      pointer-events: none;
    }
    .tts__ring circle {
      fill: none;
      stroke-width: 2;
    }
    .tts__ring-track {
      stroke: rgba(255, 255, 255, 0.35);
    }
    .tts__ring-fill {
      stroke: #fff;
      stroke-linecap: round;
      transition: stroke-dashoffset 0.25s linear;
    }
    /* Остаток времени — под кнопкой, не раздвигает строку */
    .tts__time {
      position: absolute;
      top: 100%;
      left: 50%;
      transform: translateX(-50%);
      margin-top: 2px;
      font-size: 10.5px;
      font-weight: 600;
      line-height: 1;
      color: var(--ink-3);
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    @keyframes tts-pulse {
      50% {
        opacity: 0.4;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .tts--busy {
        animation: none;
        opacity: 0.6;
      }
      .tts__ring-fill {
        transition: none;
      }
    }
  `,
})
export class TtsBtn {
  readonly player = inject(TtsPlayer);
  readonly text = input.required<string>();
  readonly group = input<readonly string[]>([]);

  readonly r = R;
  readonly circ = CIRC;

  private readonly key = computed(() => ttsKey(this.text()));
  readonly active = computed(() => this.player.current() === this.key());
  readonly playing = computed(() => this.active() && this.player.state() === 'playing');
  readonly loading = computed(() => this.active() && this.player.state() === 'loading');
  // Шаг качается фоном (прогрев) — кнопка пульсирует, как при своей загрузке.
  readonly warming = computed(() => !this.active() && this.player.warming().has(this.key()));

  readonly dashOffset = computed(() => CIRC * (1 - (this.playing() ? this.player.progress() : 0)));
  readonly remainingLabel = computed(() => {
    const s = this.player.remaining();
    if (!Number.isFinite(s)) return '';
    const total = Math.ceil(s);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  });
}
