import { Component, computed, inject, input, model, output, signal } from '@angular/core';
import { EasyWeekApi, ShoppingListItem } from '../../services/api';
import { ModelSettings } from '../../services/model-settings';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { aiFailText } from '../../shared/ai-error';
import { CookingLoader } from '../../shared/cooking-loader';
import { Modal } from '../../shared/modal';
import { ModelName } from '../../shared/model-name';

// «Свои покупки» (GUIDEBOOK «Свои покупки»): товары мимо рецептов — пишешь списком как в
// заметке («хлеб, йогурт 2 шт, молоко 1 л»), модель списка покупок раскладывает по отделам,
// и они встают в общий список с пометкой «своё». Уже добавленные — чипами, тап убирает.
// Модель — та же выпадашка, что в шапке Покупок (двусторонняя привязка), настройки не меняет.
@Component({
  selector: 'ew-shop-add',
  imports: [Modal, CookingLoader, ModelName],
  template: `
    <ew-modal label="Свои покупки" (closed)="close()">
      <div class="sa__head">
        <p class="modal__title sa__title">Свои покупки</p>
        <div class="msel">
          <button
            type="button"
            class="msel__btn"
            [class.msel__btn--open]="menuOpen()"
            [disabled]="busy()"
            aria-label="Модель списка покупок"
            (click)="menuOpen.set(!menuOpen())">
            <span class="msel__ic">🤖</span>
            <span class="msel__name">{{ label(model()) }}</span>
            <svg class="msel__chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
          @if (menuOpen()) {
            <div class="msel__backdrop" (click)="menuOpen.set(false)"></div>
            <div class="msel__menu">
              @for (m of models(); track m) {
                <button
                  type="button"
                  class="msel__opt"
                  [class.msel__opt--active]="m === model()"
                  (click)="pick(m)">
                  <span class="msel__mark msel__mark--ok">{{ m === model() ? '✓' : '' }}</span>
                  <span class="msel__opt-name"><ew-model-name [model]="ref(m)" /></span>
                  @if (m === defaultModel()) { <span class="msel__tag">по умолчанию</span> }
                </button>
              }
              <p class="msel__hint">Только для этой страницы — настройки не меняются</p>
            </div>
          }
        </div>
      </div>
      <p class="modal__text muted sa__hint">
        Что ещё купить, кроме продуктов по рецептам, — списком, как в заметке. Модель разложит
        по отделам.
      </p>
      <textarea
        class="text-field sa__text"
        rows="4"
        maxlength="2000"
        placeholder="Например: хлеб, йогурт 2 шт, молоко 1 л, бананы, губки для посуды"
        [value]="text()"
        [disabled]="busy()"
        (input)="onInput($event)"></textarea>
      @if (busy()) {
        <div class="sa__busy">
          <ew-cooking />
          <span class="muted">Раскладываю по отделам…</span>
        </div>
      } @else if (error()) {
        <p class="field-error sa__error">{{ error() }}</p>
      }
      @if (extras().length) {
        <div class="sa__added">
          <p class="sa__sub">Уже в списке — тап убирает</p>
          <div class="chip-editor">
            @for (it of extras(); track it.id) {
              <button
                class="chip"
                type="button"
                [disabled]="busy() || removing() === it.id"
                [attr.aria-label]="'Убрать ' + it.name"
                (click)="remove(it)">
                {{ it.name }}@if (it.qty) { <span class="sa__qty">{{ it.qty }} {{ it.unit }}</span> }
                <span class="chip__x">✕</span>
              </button>
            }
          </div>
        </div>
      }
      <div class="modal__actions sa__actions">
        <button class="btn-ghost" type="button" [disabled]="busy()" (click)="close()">Закрыть</button>
        <button class="btn-primary" type="button" [disabled]="!canSend()" (click)="add()">Добавить</button>
      </div>
    </ew-modal>
  `,
  styles: `
    .sa__head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }
    .sa__title {
      margin: 0;
    }
    .sa__head .msel__btn {
      background: var(--surface-sunk);
      box-shadow: none;
    }
    .sa__hint {
      margin-bottom: 12px;
    }
    .sa__text {
      display: block;
      flex-shrink: 0;
      min-height: 110px;
      padding: 12px 14px;
      border-radius: var(--r-sm);
      resize: none;
      font-family: inherit;
      line-height: 1.4;
    }
    .sa__busy {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 4px;
      margin-top: 12px;
      font-size: 13px;
    }
    .sa__error {
      margin-top: 8px;
    }
    .sa__added {
      min-height: 0;
      overflow-y: auto;
      margin-top: 14px;
    }
    .sa__sub {
      margin: 0 0 8px;
      font-size: 13px;
      font-weight: 600;
      color: var(--ink-2);
    }
    .sa__qty {
      font-weight: 500;
      color: var(--ink-2);
    }
    .sa__actions {
      flex-shrink: 0;
      margin-top: 14px;
    }
  `,
})
export class ShopAdd {
  private readonly api = inject(EasyWeekApi);
  private readonly modelSettings = inject(ModelSettings);

  readonly planId = input.required<string>();
  /** Свои товары плана (уже в списке) — чипы для удаления. */
  readonly extras = input<ShoppingListItem[]>([]);
  /** Модель разбора — общая с выпадашкой в шапке Покупок. */
  readonly model = model.required<RecipeModel>();
  /** Новый полный список своих товаров (после добавления/удаления). */
  readonly changed = output<ShoppingListItem[]>();
  /** Закрыть: тап по затемнению / Esc / «Закрыть», и сразу после успешного «Добавить». */
  readonly closed = output<void>();

  readonly text = signal('');
  readonly busy = signal(false);
  readonly removing = signal('');
  readonly error = signal('');
  readonly canSend = computed(() => !this.busy() && this.text().trim().length >= 2);

  readonly models = this.modelSettings.modelsForSignal('shopping');
  readonly defaultModel = computed(() => this.modelSettings.models().shopping);
  readonly menuOpen = signal(false);

  label(m: string): string {
    return MODEL_LABELS[m as RecipeModel] ?? m;
  }

  /** Какой конкретной моделью ответит провайдер (модель задачи «Список покупок», если тот же). */
  ref(m: string): string {
    return this.modelSettings.refFor('shopping', m);
  }

  pick(m: RecipeModel): void {
    this.model.set(m);
    this.menuOpen.set(false);
  }

  onInput(e: Event): void {
    this.text.set((e.target as HTMLTextAreaElement).value);
    this.error.set('');
  }

  // Успех — список уже на странице, окно закрываем; ошибка — текст остаётся, можно сменить модель.
  add(): void {
    if (!this.canSend()) return;
    this.busy.set(true);
    this.error.set('');
    this.api.addShoppingExtras(this.planId(), this.text().trim(), this.model()).subscribe({
      next: (list) => {
        this.busy.set(false);
        this.text.set('');
        this.changed.emit(list);
        this.closed.emit();
      },
      error: (err) => {
        this.busy.set(false);
        this.error.set(aiFailText(err, this.model(), 'список не разобран, текст на месте'));
      },
    });
  }

  remove(it: ShoppingListItem): void {
    if (!it.id || this.busy() || this.removing()) return;
    this.removing.set(it.id);
    this.error.set('');
    this.api.deleteShoppingExtra(this.planId(), it.id).subscribe({
      next: (list) => {
        this.removing.set('');
        this.changed.emit(list);
      },
      error: () => {
        this.removing.set('');
        this.error.set('Не удалось убрать — проверьте связь и повторите.');
      },
    });
  }

  close(): void {
    if (!this.busy()) this.closed.emit();
  }
}
