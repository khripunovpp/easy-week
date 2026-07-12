import { Component, DestroyRef, effect, inject, input, signal } from '@angular/core';

const DEFAULT_PHRASES = [
  'Подбираю блюда',
  'Нарезаю',
  'Тушу',
  'Помешиваю',
  'Приправляю',
  'Довожу до вкуса',
  'Раскладываю по порциям',
  'Вакуумирую',
  'Замораживаю',
];

// Кулинарный «лоадер» со сменой фраз (как ✳ Simmering… у Клода).
@Component({
  selector: 'ew-cooking',
  template: `
    <span class="cook">
      <span class="cook__star">✳</span>
      <span class="cook__text">{{ phrase() }}…</span>
    </span>
  `,
  styles: [
    `
      .cook {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: var(--ink-2);
        font-size: 15px;
        font-weight: 500;
      }
      .cook__star {
        display: inline-block;
        color: var(--accent);
        font-size: 15px;
        animation: cook-star 1.3s ease-in-out infinite;
      }
      .cook__text {
        animation: cook-fade 1.6s ease-in-out infinite;
      }
      @keyframes cook-star {
        0%,
        100% {
          transform: scale(1) rotate(0);
          opacity: 0.55;
        }
        50% {
          transform: scale(1.3) rotate(180deg);
          opacity: 1;
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
    `,
  ],
})
export class CookingLoader {
  readonly phrases = input<string[]>(DEFAULT_PHRASES);
  readonly phrase = signal(DEFAULT_PHRASES[0]);

  private idx = 0;

  constructor() {
    // показать первую фразу переданного набора
    effect(() => {
      const list = this.phrases();
      if (list.length) this.phrase.set(list[this.idx % list.length]);
    });
    const id = setInterval(() => {
      const list = this.phrases();
      if (!list.length) return;
      this.idx = (this.idx + 1) % list.length;
      this.phrase.set(list[this.idx]);
    }, 1700);
    inject(DestroyRef).onDestroy(() => clearInterval(id));
  }
}
