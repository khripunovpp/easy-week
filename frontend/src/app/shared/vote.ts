import { Component, ElementRef, computed, effect, inject, input, signal } from '@angular/core';
import { EasyWeekApi, RatingReason, RatingTarget } from '../services/api';

// Голосование 👍/👎 за ответ модели. Сам грузит текущий голос и шлёт rate (toggle/switch).
// Переиспользуется на рецепте/плане/готовке/сообщении чата.
// После 👎 — выпадашка «Что не так?» с причинами (каталог с бэка) и полем «Другое» в конце:
// фокус в поле сам отмечает «Другое». Голос уже сохранён — причины идут отдельным PUT.
@Component({
  selector: 'ew-vote',
  host: { '(document:keydown.escape)': 'closeMenu()' },
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
        aria-label="Не нравится ответ модели"
        [attr.aria-expanded]="menu() !== null">
        <svg class="vote__ic--flip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M7 22H5.5A1.5 1.5 0 0 1 4 20.5V13a1.5 1.5 0 0 1 1.5-1.5H7z" stroke-linejoin="round" />
          <path d="M7 22h8.5a2 2 0 0 0 2-1.5l2.2-7.5a1.6 1.6 0 0 0-1.5-2.1H13l.9-3.6a1.7 1.7 0 0 0-3.2-.9L7 11" stroke-linejoin="round" stroke-linecap="round" />
        </svg>
      </button>
    </div>

    @if (menu(); as pos) {
      <div class="msel__backdrop vote__backdrop" (click)="closeMenu()"></div>
      <div
        class="msel__menu vote__menu"
        role="dialog"
        aria-label="Что не так с ответом"
        [style.left.px]="pos.left"
        [style.width.px]="pos.width"
        [style.top.px]="pos.top"
        [style.bottom.px]="pos.bottom"
        [style.max-height.px]="pos.maxH">
        @if (thanks()) {
          <p class="vote__thanks">Спасибо — учтём 🙏</p>
        } @else {
          <p class="vote__title">Что не так?</p>
          @for (r of options(); track r.key) {
            <button
              type="button"
              class="msel__opt"
              [class.msel__opt--active]="picked().has(r.key)"
              [attr.aria-pressed]="picked().has(r.key)"
              (click)="toggle(r.key)">
              <span class="msel__opt-name">{{ r.label }}</span>
              <span class="msel__mark msel__mark--ok">{{ picked().has(r.key) ? '✓' : '' }}</span>
            </button>
          }
          <textarea
            class="text-field vote__note"
            [class.vote__note--on]="picked().has('other')"
            rows="2"
            maxlength="1000"
            placeholder="Другое — опишите, что не так"
            [value]="note()"
            (focus)="pickOther()"
            (input)="onNote($event)"></textarea>
          <div class="vote__actions">
            <button type="button" class="link-btn vote__skip" (click)="closeMenu()">Пропустить</button>
            <button
              type="button"
              class="btn-primary vote__send"
              [disabled]="!canSend() || sending()"
              (click)="send()">
              Отправить
            </button>
          </div>
        }
      </div>
    }
  `,
  styles: `
    .vote {
      display: inline-flex;
      gap: 4px;
    }
    .vote__btn {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 50%;
      color: var(--ink-2);
      background: var(--surface);
      box-shadow: var(--shadow-soft);
      transition: transform 0.12s ease, color 0.15s ease, background 0.15s ease;
    }
    .vote__btn svg {
      width: 18px;
      height: 18px;
      display: block;
    }
    /* 👎 — тот же значок, повёрнутый на 180°: идентичная геометрия и вертикальный центр */
    .vote__ic--flip {
      transform: rotate(180deg);
    }
    .vote__btn:active {
      transform: scale(0.9);
    }
    .vote__btn--up {
      color: #fff;
      background: var(--ok);
    }
    .vote__btn--down {
      color: #fff;
      background: var(--no);
    }

    /* Выпадашка причин: карточка .msel__menu, но fixed — кнопки стоят и слева, и справа
       (рецепт / план / тултип в чате), позицию считаем от кнопки и держим в экране. */
    .vote__backdrop {
      z-index: 60;
    }
    .vote__menu {
      position: fixed;
      right: auto;
      z-index: 61;
      min-width: 0;
      max-width: none;
      overflow-y: auto;
      overscroll-behavior: contain;
      animation: vote-menu-in 0.14s ease;
    }
    @keyframes vote-menu-in {
      from {
        opacity: 0;
      }
    }
    .vote__title {
      margin: 0;
      padding: 8px 10px 4px;
      font-size: 12px;
      font-weight: 600;
      color: var(--ink-3);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .vote__note {
      display: block;
      margin: 4px 0 0;
      padding: 10px 12px;
      border-radius: var(--r-sm);
      resize: none;
      font-family: inherit;
      line-height: 1.35;
    }
    /* «Другое» отмечено — подложка как у активного пункта */
    .vote__note--on {
      background: var(--accent-soft);
    }
    .vote__actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 4px 4px 10px;
    }
    .vote__send {
      width: auto;
      padding: 10px 18px;
      font-size: 14.5px;
    }
    .vote__send:disabled {
      opacity: 0.55;
    }
    .vote__thanks {
      margin: 0;
      padding: 14px 10px;
      font-size: 14.5px;
      font-weight: 600;
      text-align: center;
    }
  `,
})
export class Vote {
  private readonly api = inject(EasyWeekApi);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly targetType = input.required<RatingTarget>();
  readonly targetId = input.required<string>();
  readonly model = input<string>('');
  readonly planId = input<string>('');
  readonly dishId = input<string>('');
  readonly conversationId = input<string>('');

  readonly vote = signal(0);
  private loadedKey = '';

  // --- Выпадашка причин 👎 ---
  /** Позиция открытой выпадашки (null — закрыта). top ИЛИ bottom — вниз/вверх от кнопки. */
  readonly menu = signal<{
    left: number;
    width: number;
    top: number | null;
    bottom: number | null;
    maxH: number;
  } | null>(null);
  private readonly catalog = signal<RatingReason[]>([]);
  /** Пункты без «Другое» — оно всегда последним, полем ввода. */
  readonly options = computed(() => this.catalog().filter((r) => r.key !== 'other'));
  readonly picked = signal<ReadonlySet<string>>(new Set());
  readonly note = signal('');
  readonly sending = signal(false);
  readonly thanks = signal(false);
  /** Есть что отправить: причина из списка или текст (пустое «Другое» — не в счёт). */
  readonly canSend = computed(
    () => [...this.picked()].some((k) => k !== 'other') || this.note().trim().length > 0,
  );

  constructor() {
    // Прогрев каталога причин (кэш в API-сервисе, один запрос на сессию) — к первому 👎
    // выпадашка открывается без ожидания сети. Не на старте приложения: там может быть /login.
    this.api.ratingReasons().subscribe({ error: () => {} });
    // Грузим текущий голос при смене цели/модели (один раз на комбинацию).
    effect(() => {
      const id = this.targetId();
      const m = this.model();
      if (!id) return;
      const key = `${this.targetType()}|${id}|${m}`;
      if (key === this.loadedKey) return;
      this.loadedKey = key;
      this.vote.set(0);
      this.closeMenu();
      this.api.rating(this.targetType(), id, m).subscribe({
        next: (r) => this.vote.set(r.vote),
        error: () => {},
      });
    });
  }

  cast(v: 1 | -1): void {
    this.closeMenu();
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
      .subscribe({
        next: (r) => {
          this.vote.set(r.vote);
          if (r.vote === -1) this.openMenu();
        },
        error: () => {},
      });
  }

  private openMenu(): void {
    this.picked.set(new Set());
    this.note.set('');
    this.thanks.set(false);
    this.api.ratingReasons().subscribe({
      next: (cat) => {
        const items = cat[this.targetType()] ?? [];
        if (!items.length) return;
        this.catalog.set(items);
        this.menu.set(this.place());
      },
      error: () => {},
    });
  }

  /** Меню у кнопок: по правому краю, в пределах экрана; вниз, если места хватает, иначе вверх. */
  private place() {
    const r = this.host.nativeElement.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const gap = 8;
    const edge = 16;
    const width = Math.min(320, vw - edge * 2);
    const left = Math.max(edge, Math.min(r.right - width, vw - width - edge));
    const below = vh - r.bottom - gap - edge;
    const above = r.top - gap - edge;
    const down = below >= 420 || below >= above;
    return down
      ? { left, width, top: r.bottom + gap, bottom: null, maxH: below }
      : { left, width, top: null, bottom: vh - r.top + gap, maxH: above };
  }

  closeMenu(): void {
    this.menu.set(null);
  }

  toggle(key: string): void {
    this.picked.update((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  /** Фокус в поле «Другое» — отмечаем эту опцию. */
  pickOther(): void {
    if (!this.picked().has('other')) this.toggle('other');
  }

  onNote(e: Event): void {
    this.note.set((e.target as HTMLTextAreaElement).value);
  }

  send(): void {
    if (!this.canSend()) return;
    const note = this.note().trim();
    // Пустое «Другое» без текста — не причина.
    const reasons = [...this.picked()].filter((k) => k !== 'other' || note);
    this.sending.set(true);
    this.api
      .setRatingReasons({
        targetType: this.targetType(),
        targetId: this.targetId(),
        model: this.model(),
        reasons,
        note,
      })
      .subscribe({
        next: () => {
          this.sending.set(false);
          this.thanks.set(true);
          setTimeout(() => this.closeMenu(), 900);
        },
        error: () => this.sending.set(false),
      });
  }
}
