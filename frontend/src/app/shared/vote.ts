import { Component, DestroyRef, computed, effect, inject, input, signal } from '@angular/core';
import { EasyWeekApi, RatingReason, RatingTarget } from '../services/api';
import { Modal } from './modal';

// Голосование 👍/👎 за ответ модели. Сам грузит текущий голос и шлёт rate (toggle/switch).
// Переиспользуется на рецепте/плане/готовке/сообщении чата.
// После 👎 — модалка «Что не так?» с причинами (каталог с бэка) и полем «Другое» в конце:
// фокус в поле сам отмечает «Другое». Порядок: клик → сразу состояние + модалка →
// фоном POST голоса → после него PATCH причин.
// Менять голос можно 30 минут от первого голоса (как на бэке), потом кнопки блокируются.
// Модалка — общий ew-modal (поверх всего, вписан в видимую часть экрана над клавиатурой iOS).
@Component({
  selector: 'ew-vote',
  imports: [Modal],
  template: `
    <div class="vote">
      <button
        type="button"
        class="vote__btn"
        [class.vote__btn--up]="vote() === 1"
        [disabled]="locked()"
        [attr.title]="locked() ? lockedHint : null"
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
        [disabled]="locked()"
        [attr.title]="locked() ? lockedHint : null"
        (click)="cast(-1)"
        aria-label="Не нравится ответ модели"
        aria-haspopup="dialog">
        <svg class="vote__ic--flip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M7 22H5.5A1.5 1.5 0 0 1 4 20.5V13a1.5 1.5 0 0 1 1.5-1.5H7z" stroke-linejoin="round" />
          <path d="M7 22h8.5a2 2 0 0 0 2-1.5l2.2-7.5a1.6 1.6 0 0 0-1.5-2.1H13l.9-3.6a1.7 1.7 0 0 0-3.2-.9L7 11" stroke-linejoin="round" stroke-linecap="round" />
        </svg>
      </button>
    </div>

    @if (open()) {
      <ew-modal (closed)="closeMenu()">
        @if (thanks()) {
          <p class="vote__thanks">Спасибо — учтём 🙏</p>
        } @else {
          <p class="modal__title">Что не так?</p>
          <p class="modal__text muted vote__hint">Отметьте одно или несколько — это поможет модели.</p>
          <div class="vote__list">
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
          </div>
          <div class="modal__actions vote__actions">
            <button type="button" class="btn-ghost" (click)="closeMenu()">Пропустить</button>
            <button type="button" class="btn-primary" [disabled]="!canSend()" (click)="send()">
              Отправить
            </button>
          </div>
        }
      </ew-modal>
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
    /* Окно правки прошло: голос виден, но не нажимается; невыбранная кнопка — приглушена */
    .vote__btn:disabled {
      transform: none;
      cursor: default;
    }
    .vote__btn:disabled:not(.vote__btn--up):not(.vote__btn--down) {
      opacity: 0.45;
    }
    .vote__btn--up {
      color: #fff;
      background: var(--ok);
    }
    .vote__btn--down {
      color: #fff;
      background: var(--no);
    }

    .vote__hint {
      margin: 0 0 10px;
    }
    .vote__list {
      flex: 1 1 auto;
      min-height: 0;
      margin: 0 -6px;
      padding: 0 6px;
      overflow-y: auto;
      overscroll-behavior: contain;
    }
    .vote__note {
      display: block;
      margin: 6px 0 2px;
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
      flex-shrink: 0;
      margin-top: 14px;
    }
    .vote__actions .btn-primary:disabled {
      opacity: 0.55;
    }
    .vote__thanks {
      margin: 0;
      padding: 10px 0;
      font-size: 16px;
      font-weight: 600;
      text-align: center;
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
  /** Пользователь уже кликнул — поздний ответ GET-а текущего голоса не должен перетереть клик. */
  private touched = false;
  /** Запрос с голосом — причины шлём после него (без 👎 на сервере PATCH вернёт 409). */
  private voteSent: Promise<unknown> = Promise.resolve();

  // --- Окно правки голоса ---
  readonly lockedHint = 'Оценку можно менять только 30 минут';
  private static readonly EDIT_WINDOW_MS = 30 * 60_000;
  /** Когда блокируется голос (мс); 0 — голоса нет. */
  private locksAt = 0;
  readonly locked = signal(false);
  private lockTimer?: ReturnType<typeof setTimeout>;

  // --- Модалка причин 👎 ---
  readonly open = signal(false);
  private readonly catalog = signal<RatingReason[]>([]);
  /** Пункты без «Другое» — оно всегда последним, полем ввода. */
  readonly options = computed(() => this.catalog().filter((r) => r.key !== 'other'));
  readonly picked = signal<ReadonlySet<string>>(new Set());
  readonly note = signal('');
  readonly thanks = signal(false);
  /** Есть что отправить: причина из списка или текст (пустое «Другое» — не в счёт). */
  readonly canSend = computed(
    () => [...this.picked()].some((k) => k !== 'other') || this.note().trim().length > 0,
  );

  constructor() {
    // Прогрев каталога причин (кэш в API-сервисе, один запрос на сессию) — к первому 👎
    // модалка открывается без ожидания сети. Не на старте приложения: там может быть /login.
    this.api.ratingReasons().subscribe({ error: () => {} });
    inject(DestroyRef).onDestroy(() => clearTimeout(this.lockTimer));

    // Грузим текущий голос при смене цели/модели (один раз на комбинацию).
    effect(() => {
      const id = this.targetId();
      const m = this.model();
      if (!id) return;
      const key = `${this.targetType()}|${id}|${m}`;
      if (key === this.loadedKey) return;
      this.loadedKey = key;
      this.vote.set(0);
      this.setLock(0);
      this.touched = false;
      this.closeMenu();
      this.api.rating(this.targetType(), id, m).subscribe({
        next: (r) => {
          if (this.touched) return;
          this.vote.set(r.vote);
          this.setLock(r.locksAt ? Date.parse(r.locksAt) : 0);
        },
        error: () => {},
      });
    });
  }

  // Оптимистично: сразу подсвечиваем кнопку (та же логика, что на бэке: повтор — снять,
  // противоположный — переключить) и открываем причины, запрос — фоном. Упал — не страшно:
  // после обновления страницы голос подтянется с сервера как есть.
  cast(v: 1 | -1): void {
    if (this.locked()) return;
    this.touched = true;
    const next = this.vote() === v ? 0 : v;
    this.vote.set(next);
    // Снятый голос — окна нет; первый голос — окно с этого момента; переключение окно не продлевает.
    if (next === 0) this.setLock(0);
    else if (!this.locksAt) this.setLock(Date.now() + Vote.EDIT_WINDOW_MS);
    if (next === -1) this.openMenu();
    else this.closeMenu();
    const req = this.api.rate({
      targetType: this.targetType(),
      targetId: this.targetId(),
      model: this.model(),
      vote: v,
      planId: this.planId() || undefined,
      dishId: this.dishId() || undefined,
      conversationId: this.conversationId() || undefined,
    });
    this.voteSent = new Promise<void>((done) =>
      req.subscribe({
        // Серверное время окна точнее локального — подхватываем, но голос не откатываем.
        next: (r) => {
          if (r.locksAt) this.setLock(Date.parse(r.locksAt));
        },
        complete: done,
        error: () => done(),
      }),
    );
  }

  /** Выставить момент блокировки и таймер на него (страница может быть открыта долго). */
  private setLock(at: number): void {
    clearTimeout(this.lockTimer);
    this.locksAt = at;
    const left = at - Date.now();
    this.locked.set(at > 0 && left <= 0);
    if (at > 0 && left > 0) {
      this.lockTimer = setTimeout(() => {
        this.locked.set(true);
        this.closeMenu();
      }, left);
    }
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
        this.open.set(true);
      },
      error: () => {},
    });
  }

  closeMenu(): void {
    this.open.set(false);
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
    const body = {
      targetType: this.targetType(),
      targetId: this.targetId(),
      model: this.model(),
      reasons,
      note,
    };
    // Тоже оптимистично: сразу «Спасибо», PATCH причин — после того как дошёл сам 👎.
    this.thanks.set(true);
    setTimeout(() => this.closeMenu(), 900);
    this.voteSent.then(() => this.api.setRatingReasons(body).subscribe({ error: () => {} }));
  }
}
