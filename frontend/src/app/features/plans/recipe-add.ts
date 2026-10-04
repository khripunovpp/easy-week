import { Component, computed, inject, output, signal } from '@angular/core';
import { EasyWeekApi } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';
import { Modal } from '../../shared/modal';

// «Свой рецепт» (GUIDEBOOK «Свой рецепт»): пишешь рецепт как есть → «Улучшить» (текст в поле
// подменяется приведённым в порядок, рядом «Откатить») → «Дальше» — полный рецепт под заморозку
// строго по тексту, сохраняется в «Мои рецепты» и открывается его страница.
@Component({
  selector: 'ew-recipe-add',
  imports: [Modal, CookingLoader],
  template: `
    <ew-modal label="Свой рецепт" (closed)="close()">
      <p class="modal__title">Свой рецепт</p>
      <p class="modal__text muted ra__hint">
        Опишите рецепт как есть — состав и как готовить. «Улучшить» приведёт текст в порядок,
        «Дальше» соберёт полный рецепт под заморозку.
      </p>
      <textarea
        class="text-field ra__text"
        rows="10"
        maxlength="4000"
        placeholder="Например: сырники — творог 500 г, яйцо, 3 ложки муки, сахар. Смешать, сформовать, обжарить до корочки…"
        [value]="text()"
        [disabled]="!!busy()"
        (input)="onInput($event)"></textarea>
      @if (busy(); as b) {
        <div class="ra__busy">
          <ew-cooking />
          <span class="muted">{{ b === 'improve' ? 'Привожу текст в порядок…' : 'Собираю рецепт, это около минуты…' }}</span>
        </div>
      } @else if (error()) {
        <p class="field-error ra__error">{{ error() }}</p>
      }
      <div class="modal__actions ra__actions">
        @if (original() !== null) {
          <button class="btn-ghost" type="button" [disabled]="!!busy()" (click)="revert()">Откатить</button>
        } @else {
          <button class="btn-ghost" type="button" [disabled]="!canSend()" (click)="improve()">Улучшить</button>
        }
        <button class="btn-primary" type="button" [disabled]="!canSend()" (click)="next()">Дальше</button>
      </div>
    </ew-modal>
  `,
  styles: `
    .ra__hint {
      margin-bottom: 12px;
    }
    .ra__text {
      display: block;
      flex-shrink: 0;
      min-height: 180px;
      padding: 12px 14px;
      border-radius: var(--r-sm);
      resize: none;
      font-family: inherit;
      line-height: 1.4;
    }
    .ra__busy {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 4px;
      margin-top: 12px;
      font-size: 13px;
    }
    .ra__error {
      margin-top: 8px;
    }
    .ra__actions {
      flex-shrink: 0;
      margin-top: 14px;
    }
  `,
})
export class RecipeAdd {
  private readonly api = inject(EasyWeekApi);

  /** Рецепт собран и сохранён — родитель открывает его страницу. */
  readonly created = output<{ planId: string; dishId: string }>();
  /** Закрыть модалку (тап по затемнению / Esc), пока ничего не генерится. */
  readonly closed = output<void>();

  readonly text = signal('');
  /** Текст до «Улучшить» — для «Откатить»; null — улучшения не было. */
  readonly original = signal<string | null>(null);
  readonly busy = signal<'improve' | 'create' | null>(null);
  readonly error = signal('');
  readonly canSend = computed(() => !this.busy() && this.text().trim().length >= 3);

  onInput(e: Event): void {
    this.text.set((e.target as HTMLTextAreaElement).value);
    this.error.set('');
  }

  improve(): void {
    if (!this.canSend()) return;
    const before = this.text();
    this.busy.set('improve');
    this.error.set('');
    this.api.improveRecipe(before).subscribe({
      next: (res) => {
        this.original.set(before);
        this.text.set(res.text);
        this.busy.set(null);
      },
      error: (err) => this.fail(err, 'Не удалось улучшить текст. Попробуйте ещё раз.'),
    });
  }

  revert(): void {
    const before = this.original();
    if (before === null) return;
    this.text.set(before);
    this.original.set(null);
  }

  next(): void {
    if (!this.canSend()) return;
    this.busy.set('create');
    this.error.set('');
    this.api.createRecipe(this.text()).subscribe({
      next: (res) => {
        this.busy.set(null);
        this.created.emit(res);
      },
      error: (err) => this.fail(err, 'Не удалось собрать рецепт. Попробуйте ещё раз.'),
    });
  }

  close(): void {
    if (!this.busy()) this.closed.emit();
  }

  private fail(err: unknown, fallback: string): void {
    this.busy.set(null);
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    this.error.set(typeof detail === 'string' && detail ? detail : fallback);
  }
}
