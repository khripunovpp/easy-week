import { Component, computed, inject, linkedSignal, output, signal } from '@angular/core';
import { EasyWeekApi } from '../../services/api';
import { ModelSettings } from '../../services/model-settings';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { Modal } from '../../shared/modal';
import { ModelName } from '../../shared/model-name';

// «Свой рецепт» (GUIDEBOOK «Свой рецепт»): пишешь рецепт как есть → «Улучшить» (текст в поле
// подменяется приведённым в порядок, рядом «Откатить») → «Дальше» — полный рецепт под заморозку
// строго по тексту, сохраняется в «Мои рецепты» и открывается его страница. Модель — выпадашка
// в шапке (как на других страницах): по умолчанию — задача «Рецепты», выбор только для этого рецепта.
@Component({
  selector: 'ew-recipe-add',
  imports: [Modal, CookingLoader, ModelName],
  template: `
    <ew-modal label="Свой рецепт" (closed)="close()">
      <div class="ra__head">
        <p class="modal__title ra__title">Свой рецепт</p>
        <div class="msel">
          <button
            type="button"
            class="msel__btn"
            [class.msel__btn--open]="menuOpen()"
            [disabled]="!!busy()"
            aria-label="Модель рецепта"
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
              <p class="msel__hint">Только для этого рецепта — настройки не меняются</p>
            </div>
          }
        </div>
      </div>
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
    .ra__head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }
    .ra__title {
      margin: 0;
    }
    .ra__head .msel__btn {
      background: var(--surface-sunk);
      box-shadow: none;
    }
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
  private readonly modelSettings = inject(ModelSettings);

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

  // Модель «Улучшить»/«Дальше»: стартует с дефолта «Рецепты», выбор здесь настройки не меняет.
  readonly models = this.modelSettings.modelsForSignal('recipe');
  readonly defaultModel = computed(() => this.modelSettings.models().recipe);
  readonly model = linkedSignal<RecipeModel>(() => this.defaultModel());
  readonly menuOpen = signal(false);

  constructor() {
    this.modelSettings.ensureLoaded();
  }

  label(m: string): string {
    return MODEL_LABELS[m as RecipeModel] ?? m;
  }

  /** Какой конкретной моделью ответит провайдер (модель задачи «Рецепты», если тот же). */
  ref(m: string): string {
    return this.modelSettings.refFor('recipe', m);
  }

  pick(m: RecipeModel): void {
    this.model.set(m);
    this.menuOpen.set(false);
  }

  onInput(e: Event): void {
    this.text.set((e.target as HTMLTextAreaElement).value);
    this.error.set('');
  }

  improve(): void {
    if (!this.canSend()) return;
    const before = this.text();
    this.busy.set('improve');
    this.error.set('');
    this.api.improveRecipe(before, this.model()).subscribe({
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
    this.api.createRecipe(this.text(), this.model()).subscribe({
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
