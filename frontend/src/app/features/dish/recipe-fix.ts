import { Component, computed, inject, input, linkedSignal, output, signal } from '@angular/core';
import { Dish } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';
import { ModelSettings } from '../../services/model-settings';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { aiFailText } from '../../shared/ai-error';
import { CookingLoader, LoaderModel } from '../../shared/cooking-loader';
import { Modal } from '../../shared/modal';
import { ModelName } from '../../shared/model-name';

// «Исправить» (GUIDEBOOK «Исправить рецепт»): точечная правка рецепта без перегенерации —
// «убери лук»: модель задачи «Правка рецепта» меняет только нужные строки ингредиентов, шагов,
// советов и описания, остальное дословно. Модель — выпадашка в шапке окна (как «Свой рецепт»),
// выбор только для этой правки.
@Component({
  selector: 'ew-recipe-fix',
  imports: [Modal, CookingLoader, ModelName],
  template: `
    <ew-modal label="Исправить рецепт" (closed)="close()">
      <div class="rf__head">
        <p class="modal__title rf__title">Исправить</p>
        <div class="msel">
          <button
            type="button"
            class="msel__btn"
            [class.msel__btn--open]="menuOpen()"
            [disabled]="busy()"
            aria-label="Модель правки"
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
              <p class="msel__hint">Только для этой правки — настройки не меняются</p>
            </div>
          }
        </div>
      </div>
      <p class="modal__text muted rf__hint">
        Что поменять? Модель уберёт или заменит только это — в ингредиентах, шагах и описании,
        остальной рецепт не трогает.
      </p>
      <textarea
        class="text-field rf__text"
        rows="3"
        maxlength="500"
        placeholder="Например: убери лук"
        [value]="text()"
        [disabled]="busy()"
        (input)="onInput($event)"></textarea>
      @if (busy()) {
        <div class="rf__busy"><ew-cooking [models]="loaderModels()" /></div>
      } @else if (error()) {
        <p class="field-error rf__error">{{ error() }}</p>
      }
      <div class="modal__actions rf__actions">
        <button class="btn-ghost" type="button" [disabled]="busy()" (click)="close()">Отмена</button>
        <button class="btn-primary" type="button" [disabled]="!canSend()" (click)="send()">Исправить</button>
      </div>
    </ew-modal>
  `,
  styles: `
    .rf__head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }
    .rf__title {
      margin: 0;
    }
    .rf__head .msel__btn {
      background: var(--surface-sunk);
      box-shadow: none;
    }
    .rf__hint {
      margin-bottom: 12px;
    }
    .rf__text {
      display: block;
      flex-shrink: 0;
      padding: 12px 14px;
      border-radius: var(--r-sm);
      resize: none;
      font-family: inherit;
      line-height: 1.4;
    }
    .rf__busy {
      display: flex;
      justify-content: center;
      margin-top: 12px;
    }
    .rf__error {
      margin-top: 8px;
    }
    .rf__actions {
      flex-shrink: 0;
      margin-top: 14px;
    }
  `,
})
export class RecipeFix {
  private readonly api = inject(EasyWeekApi);
  private readonly modelSettings = inject(ModelSettings);

  readonly planId = input.required<string>();
  readonly dishId = input.required<string>();
  /** Правка записана: новое блюдо + что сделала модель (одной фразой). */
  readonly fixed = output<{ dish: Dish; reply: string }>();
  readonly closed = output<void>();

  readonly text = signal('');
  readonly busy = signal(false);
  readonly error = signal('');
  readonly canSend = computed(() => !this.busy() && this.text().trim().length >= 2);

  readonly models = this.modelSettings.modelsForSignal('fix');
  readonly defaultModel = computed(() => this.modelSettings.models().fix);
  readonly model = linkedSignal<RecipeModel>(() => this.defaultModel());
  readonly menuOpen = signal(false);
  readonly loaderModels = computed<LoaderModel[]>(() => [{ note: 'Правит', model: this.ref(this.model()) }]);

  constructor() {
    this.modelSettings.ensureLoaded();
  }

  label(m: string): string {
    return MODEL_LABELS[m as RecipeModel] ?? m;
  }

  ref(m: string): string {
    return this.modelSettings.refFor('fix', m);
  }

  pick(m: RecipeModel): void {
    this.model.set(m);
    this.menuOpen.set(false);
  }

  onInput(e: Event): void {
    this.text.set((e.target as HTMLTextAreaElement).value);
    this.error.set('');
  }

  send(): void {
    if (!this.canSend()) return;
    this.busy.set(true);
    this.error.set('');
    this.api.fixDish(this.planId(), this.dishId(), this.text().trim(), this.model()).subscribe({
      next: (res) => {
        this.busy.set(false);
        this.fixed.emit(res);
      },
      error: (err) => {
        this.busy.set(false);
        const e = err as { status?: number; error?: { detail?: unknown } };
        const detail = typeof e?.error?.detail === 'string' ? e.error.detail : '';
        // 422 — модель не нашла, что менять (её объяснение); 409 — рецепт поменяли параллельно.
        this.error.set(
          (e?.status === 422 || e?.status === 409) && detail
            ? detail
            : aiFailText(err, this.model(), 'рецепт не исправлен, текст на месте'),
        );
      },
    });
  }

  close(): void {
    if (!this.busy()) this.closed.emit();
  }
}
