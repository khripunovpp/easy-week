import { Component, OnInit, inject, output } from '@angular/core';
import { PlanWizard } from '../../shared/plan-wizard';

// Карточка быстрого выбора в новом чате (GUIDEBOOK → «Быстрый выбор плана»): шаг за шагом,
// одиночный выбор ведёт дальше сам, мультивыбор — «Дальше». В конце — итог-строка и
// «Составить план»; дописать можно в поле ввода внизу (текст важнее карточек).
@Component({
  selector: 'ew-plan-wizard',
  template: `
    <section class="wiz" aria-label="Быстрый выбор плана">
      @if (w.current(); as step) {
        <div class="wiz__head">
          <span class="wiz__step">Шаг {{ w.index() + 1 }} из {{ w.steps().length }}</span>
          @if (w.choice()) {
            <button class="link-btn" type="button" (click)="build.emit()">Составить план</button>
          }
        </div>
        <p class="wiz__q">{{ step.title }}</p>
        @if (step.multi) {
          <p class="wiz__hint">Можно несколько</p>
        }
        <div class="chip-editor wiz__opts">
          @for (opt of step.options; track opt.id) {
            <button
              class="chip wiz__opt"
              type="button"
              [class.chip--yes]="w.isOn(step, opt.id)"
              [attr.aria-pressed]="w.isOn(step, opt.id)"
              (click)="w.pick(step, opt.id)">
              @if (w.isOn(step, opt.id)) { ✓ }{{ opt.label }}
            </button>
          }
        </div>
        <div class="wiz__nav">
          @if (w.index() > 0) {
            <button class="link-btn wiz__link" type="button" (click)="w.back()">← Назад</button>
          } @else {
            <span></span>
          }
          <span class="wiz__nav-right">
            <button class="link-btn wiz__link wiz__link--muted" type="button" (click)="w.skip()">Пропустить</button>
            @if (step.multi) {
              <button class="link-btn wiz__link" type="button" [disabled]="!anyOn(step)" (click)="w.next()">Дальше →</button>
            }
          </span>
        </div>
      } @else {
        <p class="wiz__q">Готово</p>
        @if (w.choice()) {
          <p class="wiz__summary">{{ w.choice() }}</p>
          <p class="wiz__hint">Можно дописать уточнение в поле внизу — оно важнее выбора.</p>
        } @else {
          <p class="wiz__hint">Ничего не выбрано — напишите, что хотите, в поле внизу.</p>
        }
        <div class="wiz__row">
          @if (!w.withExtra()) {
            <button class="btn-ghost" type="button" (click)="w.more()">Ещё вопросы</button>
          } @else {
            <button class="btn-ghost" type="button" (click)="w.edit()">Изменить</button>
          }
          <button class="btn-primary" type="button" [disabled]="!w.choice()" (click)="build.emit()">Составить план</button>
        </div>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      align-self: stretch;
    }
    .wiz {
      padding: 14px 16px 12px;
      border-radius: var(--r-md);
      background: var(--surface);
      box-shadow: var(--shadow-soft);
    }
    .wiz__head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 20px;
    }
    .wiz__step {
      font-size: 12px;
      font-weight: 600;
      color: var(--ink-3);
    }
    .wiz__q {
      margin: 6px 0 2px;
      font-size: 16px;
      font-weight: 700;
      color: var(--ink);
    }
    .wiz__hint {
      margin: 0 0 4px;
      font-size: 13px;
      color: var(--ink-2);
    }
    .wiz__opts {
      margin: 10px 0 8px;
    }
    .wiz__opt:not(.chip--yes) {
      background: var(--surface-sunk);
      color: var(--ink);
    }
    .wiz__nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 32px;
    }
    .wiz__nav-right {
      display: inline-flex;
      gap: 16px;
    }
    .wiz__link {
      padding: 6px 0;
      font-size: 14px;
    }
    .wiz__link--muted {
      color: var(--ink-3);
    }
    .wiz__link:disabled {
      opacity: 0.45;
    }
    .wiz__summary {
      margin: 8px 0 6px;
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--accent-soft);
      color: var(--ink);
      font-size: 14.5px;
      font-weight: 600;
      line-height: 1.4;
    }
    .wiz__row {
      display: flex;
      gap: 10px;
      margin-top: 10px;
    }
    .wiz__row > .btn-primary,
    .wiz__row > .btn-ghost {
      flex: 1;
      width: auto;
      min-width: 0;
      padding: 13px 12px;
      font-size: 15px;
      white-space: nowrap;
    }
  `,
})
export class PlanWizardCard implements OnInit {
  readonly w = inject(PlanWizard);
  /** «Составить план» — родитель отправляет сообщение (выбор + дописанный текст). */
  readonly build = output<void>();

  ngOnInit(): void {
    this.w.loadFavorites(); // для «Проверенное» — повторить что-то из избранного
  }

  anyOn(step: { id: string }): boolean {
    return (this.w.answers()[step.id] ?? []).length > 0;
  }
}
