import {
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { EasyWeekApi, RatingReason, RatingTarget } from '../services/api';

// Голосование 👍/👎 за ответ модели. Сам грузит текущий голос и шлёт rate (toggle/switch).
// Переиспользуется на рецепте/плане/готовке/сообщении чата.
// После 👎 — модалка «Что не так?» с причинами (каталог с бэка) и полем «Другое» в конце:
// фокус в поле сам отмечает «Другое». Порядок: клик → сразу состояние + модалка →
// фоном POST голоса → после него PATCH причин.
// Модалка — нативный <dialog> (showModal → top layer поверх всего, Esc закрывает сам браузер).
// Высоту подгоняем под visualViewport: на iPhone клавиатура не перекрывает карточку.
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
        aria-label="Не нравится ответ модели"
        aria-haspopup="dialog">
        <svg class="vote__ic--flip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M7 22H5.5A1.5 1.5 0 0 1 4 20.5V13a1.5 1.5 0 0 1 1.5-1.5H7z" stroke-linejoin="round" />
          <path d="M7 22h8.5a2 2 0 0 0 2-1.5l2.2-7.5a1.6 1.6 0 0 0-1.5-2.1H13l.9-3.6a1.7 1.7 0 0 0-3.2-.9L7 11" stroke-linejoin="round" stroke-linecap="round" />
        </svg>
      </button>
    </div>

    @if (open()) {
      <dialog
        #dlg
        class="modal"
        aria-label="Что не так с ответом"
        [style.top.px]="vp().top"
        [style.height.px]="vp().height"
        (click)="onBackdrop($event)"
        (close)="open.set(false)">
        <div class="modal__card vote__card">
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
        </div>
      </dialog>
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

    /* Модалка на видимую область (top/height — из visualViewport), паддинг меньше — больше места
       списку; карточка колонкой: шапка и кнопки на месте, скроллится только список. */
    .modal {
      bottom: auto;
      padding: 16px;
    }
    .vote__card {
      display: flex;
      flex-direction: column;
      max-width: 380px;
      max-height: 100%;
      padding: 20px 16px 16px;
    }
    .vote__hint {
      margin: 0 0 10px;
      padding: 0 2px;
    }
    .modal__title {
      padding: 0 2px;
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
    .vote__actions .btn-ghost,
    .vote__actions .btn-primary {
      padding: 14px 16px;
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

  // --- Модалка причин 👎 ---
  private readonly dlg = viewChild<ElementRef<HTMLDialogElement>>('dlg');
  readonly open = signal(false);
  /** Видимая область экрана (без клавиатуры на iOS) — в неё вписываем модалку. */
  readonly vp = signal({ top: 0, height: window.innerHeight });
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

    // Отрисовали <dialog> — открываем модально (top layer). Без API — останется fixed-оверлеем.
    effect(() => {
      const el = this.dlg()?.nativeElement;
      if (el && !el.open) {
        if (el.showModal) el.showModal();
        else el.setAttribute('open', '');
      }
    });

    // Клавиатура iOS сжимает visualViewport, а не окно — держим модалку в видимой части.
    const vv = window.visualViewport;
    const fit = () => this.fit();
    vv?.addEventListener('resize', fit);
    vv?.addEventListener('scroll', fit);
    window.addEventListener('resize', fit);
    inject(DestroyRef).onDestroy(() => {
      vv?.removeEventListener('resize', fit);
      vv?.removeEventListener('scroll', fit);
      window.removeEventListener('resize', fit);
    });

    // Грузим текущий голос при смене цели/модели (один раз на комбинацию).
    effect(() => {
      const id = this.targetId();
      const m = this.model();
      if (!id) return;
      const key = `${this.targetType()}|${id}|${m}`;
      if (key === this.loadedKey) return;
      this.loadedKey = key;
      this.vote.set(0);
      this.touched = false;
      this.closeMenu();
      this.api.rating(this.targetType(), id, m).subscribe({
        next: (r) => {
          if (!this.touched) this.vote.set(r.vote);
        },
        error: () => {},
      });
    });
  }

  private fit(): void {
    const vv = window.visualViewport;
    this.vp.set(vv ? { top: vv.offsetTop, height: vv.height } : { top: 0, height: window.innerHeight });
  }

  // Оптимистично: сразу подсвечиваем кнопку (та же логика, что на бэке: повтор — снять,
  // противоположный — переключить) и открываем причины, запрос — фоном. Упал — не страшно:
  // после обновления страницы голос подтянется с сервера как есть.
  cast(v: 1 | -1): void {
    this.touched = true;
    const next = this.vote() === v ? 0 : v;
    this.vote.set(next);
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
      req.subscribe({ complete: done, error: () => done() }),
    );
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
        this.fit();
        this.open.set(true);
      },
      error: () => {},
    });
  }

  closeMenu(): void {
    this.open.set(false);
  }

  /** Тап по затемнению (сам <dialog>, не карточка) — закрыть. */
  onBackdrop(e: MouseEvent): void {
    if (e.target === e.currentTarget) this.closeMenu();
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
