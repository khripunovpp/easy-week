import { Component, DestroyRef, inject, signal } from '@angular/core';
import { COOKING_PHRASES } from './cooking-phrases';

// Кулинарный «лоадер»: кастрюлька с поднимающимся паром + смена фраз.
@Component({
  selector: 'ew-cooking',
  template: `
    <span class="cook">
      <svg class="cook__pot" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <g class="cook__steam" stroke="var(--accent)" stroke-width="1.6" stroke-linecap="round">
          <path class="s1" d="M9 8.5c-1-1.2.9-2.1 0-3.4" />
          <path class="s2" d="M12 8c-1-1.3.9-2.3 0-3.7" />
          <path class="s3" d="M15 8.5c-1-1.2.9-2.1 0-3.4" />
        </g>
        <g stroke="var(--ink-2)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3.5 11h17" />
          <path d="M5 11l1.1 7.4c.15 1 .9 1.6 1.9 1.6h8c1 0 1.75-.6 1.9-1.6L19 11" />
          <path d="M5 12.4c-1.4 0-2.2.7-2.2 1.7s.8 1.6 2 1.6" />
          <path d="M19 12.4c1.4 0 2.2.7 2.2 1.7s-.8 1.6-2 1.6" />
        </g>
      </svg>
      <span class="cook__text">{{ phrase() }}…</span>
    </span>
  `,
  styles: [
    `
      .cook {
        display: inline-flex;
        align-items: center;
        gap: 9px;
        color: var(--ink-2);
        font-size: 15px;
        font-weight: 500;
      }
      .cook__pot {
        width: 24px;
        height: 24px;
        flex: none;
      }
      .cook__steam path {
        transform-box: fill-box;
        transform-origin: bottom center;
        opacity: 0;
        animation: cook-steam 1.9s ease-out infinite;
      }
      .cook__steam .s1 {
        animation-delay: 0s;
      }
      .cook__steam .s2 {
        animation-delay: 0.5s;
      }
      .cook__steam .s3 {
        animation-delay: 1s;
      }
      .cook__text {
        animation: cook-fade 1.7s ease-in-out infinite;
      }
      @keyframes cook-steam {
        0% {
          opacity: 0;
          transform: translateY(2px) scaleY(0.6);
        }
        35% {
          opacity: 0.9;
        }
        100% {
          opacity: 0;
          transform: translateY(-3px) scaleY(1.15);
        }
      }
      @keyframes cook-fade {
        0%,
        100% {
          opacity: 0.65;
        }
        50% {
          opacity: 1;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .cook__steam path {
          animation: none;
          opacity: 0.7;
        }
        .cook__text {
          animation: none;
        }
      }
    `,
  ],
})
export class CookingLoader {
  // случайный старт по пулу — чтобы фразы не повторялись от раза к разу
  private idx = Math.floor(Math.random() * COOKING_PHRASES.length);
  readonly phrase = signal(COOKING_PHRASES[this.idx]);

  constructor() {
    const id = setInterval(() => {
      this.idx = (this.idx + 1) % COOKING_PHRASES.length;
      this.phrase.set(COOKING_PHRASES[this.idx]);
    }, 1700);
    inject(DestroyRef).onDestroy(() => clearInterval(id));
  }
}
