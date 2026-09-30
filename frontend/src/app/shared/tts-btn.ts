import { Component, computed, inject, input } from '@angular/core';
import { TtsPlayer, ttsKey } from './tts-player';

// Кнопка озвучки шага 🔊 (GUIDEBOOK → «Озвучка шага»): круглая 28px на --surface-sunk;
// пока аудио грузится — пульсирует; играет — коралловая заливка и значок «стоп».
// Состояние общее (TtsPlayer): играет только один шаг, тап по другому переключает.
@Component({
  selector: 'ew-tts-btn',
  template: `
    <button
      type="button"
      class="tts"
      [class.tts--on]="playing()"
      [class.tts--busy]="loading()"
      [attr.aria-label]="playing() ? 'Остановить озвучку' : 'Озвучить шаг'"
      [attr.aria-pressed]="playing() || loading()"
      [attr.title]="active() && player.error() ? player.error() : null"
      (click)="player.toggle(text())">
      @if (playing()) {
        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <rect x="6" y="6" width="12" height="12" rx="2" />
        </svg>
      } @else {
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true">
          <path d="M4 9.5v5a1 1 0 0 0 1 1h2.6l4.2 3.3a.6.6 0 0 0 1-.5V5.7a.6.6 0 0 0-1-.5L7.6 8.5H5a1 1 0 0 0-1 1z" stroke-linejoin="round" />
          <path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" stroke-linecap="round" />
        </svg>
      }
    </button>
  `,
  styles: `
    :host {
      display: inline-flex;
      flex-shrink: 0;
    }
    .tts {
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
    .tts svg {
      width: 16px;
      height: 16px;
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
    }
  `,
})
export class TtsBtn {
  readonly player = inject(TtsPlayer);
  readonly text = input.required<string>();

  readonly active = computed(() => this.player.current() === ttsKey(this.text()));
  readonly playing = computed(() => this.active() && this.player.state() === 'playing');
  readonly loading = computed(() => this.active() && this.player.state() === 'loading');
}
