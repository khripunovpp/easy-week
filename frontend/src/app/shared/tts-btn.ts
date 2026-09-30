import { Component, computed, inject, input } from '@angular/core';
import { TtsPlayer, ttsKey } from './tts-player';
import { formatClock } from './tts-panel';

// Кнопка озвучки шага 🔊 (GUIDEBOOK → «Озвучка шага»), круглая 28px. Состояния:
// не загружен — серая (--surface-sunk); грузится (свой шаг или прогрев) — пульс;
// готов — оранжевая подложка (--accent-soft, иконка --accent); играет — сплошная --accent,
// значок «пауза», белое кольцо прогресса и остаток времени под кнопкой; на паузе — подложка
// --accent-soft, кольцо --accent, значок «плей». Управление дублирует нижняя панель (ew-tts-panel).
// [group] — все шаги экрана (прогрев и «Подряд»), [source] — подпись в панели (блюдо / план).
const R = 12; // радиус кольца в viewBox 28×28 (внутри края кнопки)
const CIRC = 2 * Math.PI * R;

@Component({
  selector: 'ew-tts-btn',
  template: `
    <button
      type="button"
      class="tts"
      [class.tts--ready]="ready()"
      [class.tts--on]="playing()"
      [class.tts--paused]="paused()"
      [class.tts--busy]="loading() || warming()"
      [attr.aria-label]="playing() ? 'Пауза' : paused() ? 'Продолжить' : 'Озвучить шаг'"
      [attr.aria-pressed]="playing() || paused()"
      (click)="player.toggle(text(), group(), source())">
      @if (playing() || paused()) {
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
      }
      @if (playing()) {
        <svg class="tts__ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <rect x="7.5" y="6.5" width="3" height="11" rx="1" />
          <rect x="13.5" y="6.5" width="3" height="11" rx="1" />
        </svg>
      } @else if (paused()) {
        <svg class="tts__ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M9 7.2v9.6a.8.8 0 0 0 1.2.7l7.6-4.8a.8.8 0 0 0 0-1.4l-7.6-4.8a.8.8 0 0 0-1.2.7z" />
        </svg>
      } @else {
        <svg class="tts__ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true">
          <path d="M4 9.5v5a1 1 0 0 0 1 1h2.6l4.2 3.3a.6.6 0 0 0 1-.5V5.7a.6.6 0 0 0-1-.5L7.6 8.5H5a1 1 0 0 0-1 1z" stroke-linejoin="round" />
          <path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" stroke-linecap="round" />
        </svg>
      }
    </button>
    @if ((playing() || paused()) && remainingLabel()) {
      <span class="tts__time" aria-hidden="true">−{{ remainingLabel() }}</span>
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
    .tts--ready,
    .tts--paused {
      background: var(--accent-soft);
      color: var(--accent);
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
    /* Кольцо прогресса — по краю кнопки, от 12 часов по часовой; цвет — currentColor */
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
      stroke: currentColor;
      stroke-width: 2;
    }
    .tts__ring-track {
      opacity: 0.3;
    }
    .tts__ring-fill {
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
  readonly source = input('');

  readonly r = R;
  readonly circ = CIRC;

  private readonly key = computed(() => ttsKey(this.text()));
  readonly active = computed(() => this.player.current() === this.key());
  readonly playing = computed(() => this.active() && this.player.state() === 'playing');
  readonly paused = computed(() => this.active() && this.player.state() === 'paused');
  readonly loading = computed(() => this.active() && this.player.state() === 'loading');
  // Шаг качается фоном (прогрев) — кнопка пульсирует, как при своей загрузке.
  readonly warming = computed(() => !this.active() && this.player.warming().has(this.key()));
  readonly ready = computed(
    () => !this.playing() && !this.loading() && this.player.readyKeys().has(this.key()),
  );

  readonly dashOffset = computed(() => CIRC * (1 - this.player.progress()));
  readonly remainingLabel = computed(() => formatClock(this.player.remaining()));
}
