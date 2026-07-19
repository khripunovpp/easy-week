import { Component, effect, inject, input, signal } from '@angular/core';
import { EasyWeekApi, RatingTarget } from '../services/api';

// Голосование 👍/👎 за ответ модели. Сам грузит текущий голос и шлёт rate (toggle/switch).
// Переиспользуется на рецепте/плане/готовке/сообщении чата.
@Component({
  selector: 'ew-vote',
  template: `
    <div class="vote">
      <button
        type="button"
        class="vote__btn"
        [class.vote__btn--up]="vote() === 1"
        (click)="cast(1)"
        aria-label="Нравится ответ модели">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M7 22H5.5A1.5 1.5 0 0 1 4 20.5V13a1.5 1.5 0 0 1 1.5-1.5H7z" stroke-linejoin="round" />
          <path d="M7 22h8.5a2 2 0 0 0 2-1.5l2.2-7.5a1.6 1.6 0 0 0-1.5-2.1H13l.9-3.6a1.7 1.7 0 0 0-3.2-.9L7 11" stroke-linejoin="round" stroke-linecap="round" />
        </svg>
      </button>
      <button
        type="button"
        class="vote__btn"
        [class.vote__btn--down]="vote() === -1"
        (click)="cast(-1)"
        aria-label="Не нравится ответ модели">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M17 2H8.5a2 2 0 0 0-2 1.5L4.3 11a1.6 1.6 0 0 0 1.5 2.1H11l-.9 3.6a1.7 1.7 0 0 0 3.2.9L17 13" stroke-linejoin="round" stroke-linecap="round" />
          <path d="M17 2h1.5A1.5 1.5 0 0 1 20 3.5V11a1.5 1.5 0 0 1-1.5 1.5H17z" stroke-linejoin="round" />
        </svg>
      </button>
    </div>
  `,
  styles: `
    .vote {
      display: inline-flex;
      gap: 4px;
    }
    .vote__btn {
      display: grid;
      place-items: center;
      width: 32px;
      height: 32px;
      border-radius: 50%;
      color: var(--ink-3);
      transition: transform 0.12s ease, color 0.15s ease, background 0.15s ease;
    }
    .vote__btn svg {
      width: 18px;
      height: 18px;
    }
    .vote__btn:active {
      transform: scale(0.9);
    }
    .vote__btn--up {
      color: var(--ok);
      background: color-mix(in srgb, var(--ok) 15%, transparent);
    }
    .vote__btn--down {
      color: var(--no);
      background: color-mix(in srgb, var(--no) 15%, transparent);
    }
  `,
})
export class Vote {
  private readonly api = inject(EasyWeekApi);

  readonly targetType = input.required<RatingTarget>();
  readonly targetId = input.required<string>();
  readonly model = input<string>('');
  readonly planId = input<string>('');
  readonly dishId = input<string>('');
  readonly conversationId = input<string>('');

  readonly vote = signal(0);
  private loadedKey = '';

  constructor() {
    // Грузим текущий голос при смене цели/модели (один раз на комбинацию).
    effect(() => {
      const id = this.targetId();
      const m = this.model();
      if (!id) return;
      const key = `${this.targetType()}|${id}|${m}`;
      if (key === this.loadedKey) return;
      this.loadedKey = key;
      this.vote.set(0);
      this.api.rating(this.targetType(), id, m).subscribe({
        next: (r) => this.vote.set(r.vote),
        error: () => {},
      });
    });
  }

  cast(v: 1 | -1): void {
    this.api
      .rate({
        targetType: this.targetType(),
        targetId: this.targetId(),
        model: this.model(),
        vote: v,
        planId: this.planId() || undefined,
        dishId: this.dishId() || undefined,
        conversationId: this.conversationId() || undefined,
      })
      .subscribe({ next: (r) => this.vote.set(r.vote), error: () => {} });
  }
}
