import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

/** Лимиты — как на бэке (ai/prefs.py): пункт ≤ 40 символов, список ≤ 30. */
export const PREF_ITEM_MAX = 40;
export const PREF_LIST_MAX = 30;

/**
 * Чип-редактор (GUIDEBOOK → «Чип-редактор»): чипы с ✕ (тап — удалить) + поле добавления
 * (Enter или «Готово» на клавиатуре). Сам ничего не хранит — отдаёт add/remove наверх.
 */
@Component({
  selector: 'ew-chip-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="chip-editor">
      @for (x of items(); track x) {
        <button
          class="chip"
          [class.chip--no]="tone() === 'no'"
          [class.chip--yes]="tone() === 'yes'"
          type="button"
          [attr.aria-label]="'Удалить: ' + x"
          (click)="remove.emit(x)">
          {{ x }} <span class="chip__x" aria-hidden="true">✕</span>
        </button>
      }
      @if (items().length < max) {
        <input
          #field
          class="chip-add"
          type="text"
          enterkeyhint="done"
          autocomplete="off"
          [attr.maxlength]="itemMax"
          [placeholder]="placeholder()"
          [attr.aria-label]="label()"
          (keydown.enter)="submit(field)"
          (blur)="submit(field)" />
      } @else {
        <span class="muted chip-editor__full">Максимум {{ max }} — удалите лишнее</span>
      }
    </div>
  `,
  styles: `
    .chip-editor__full {
      font-size: 13px;
    }
  `,
})
export class ChipEditor {
  readonly items = input.required<string[]>();
  /** Тон чипов: no — аллергии, yes — любимое, plain — нейтральные. */
  readonly tone = input<'no' | 'yes' | 'plain'>('plain');
  readonly placeholder = input('добавить…');
  readonly label = input('Добавить');

  readonly add = output<string>();
  readonly remove = output<string>();

  readonly max = PREF_LIST_MAX;
  readonly itemMax = PREF_ITEM_MAX;

  /** Enter/blur: непустое значение уходит наверх, поле очищается. */
  submit(field: HTMLInputElement): void {
    const v = field.value.trim();
    field.value = '';
    if (v) this.add.emit(v);
  }
}
