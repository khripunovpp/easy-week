import { Component, ElementRef, effect, inject, input, signal, viewChild } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { PlanStatus, WeekPlan } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { providerToModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { formatDuration, formatGeneratedAt } from '../../shared/format';
import { Vote } from '../../shared/vote';

@Component({
  selector: 'ew-plan',
  imports: [RouterLink, CookingLoader, Vote],
  templateUrl: './plan.html',
  styleUrl: './plan.scss',
})
export class PlanPage {
  /** Подпись даты генерации: «сегодня, 14:05» / «26 сен, 14:05»; пусто — не показываем. */
  genAt(iso: string | null | undefined): string {
    return formatGeneratedAt(iso);
  }

  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly router = inject(Router);

  readonly id = input<string>('');

  readonly plan = signal<WeekPlan | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);

  // ---- Переименование плана ----
  /** Заголовок сейчас в режиме contenteditable. */
  readonly editingTitle = signal(false);
  private readonly titleEl = viewChild<ElementRef<HTMLElement>>('titleEl');
  private pressTimer: ReturnType<typeof setTimeout> | null = null;
  private static readonly LONG_PRESS_MS = 500;

  constructor() {
    effect(() => {
      const id = this.id();
      if (!id) return;
      this.loading.set(true);
      this.failed.set(false);
      this.api.getPlan(id).subscribe({
        next: (p) => {
          this.plan.set(p);
          this.loading.set(false);
        },
        error: () => {
          this.failed.set(true);
          this.loading.set(false);
        },
      });
    });
  }

  /** Начало нажатия на заголовок: через LONG_PRESS_MS включаем правку. */
  titlePressStart(e: PointerEvent): void {
    if (this.editingTitle() || e.button > 0) return;
    this.titlePressCancel();
    this.pressTimer = setTimeout(() => {
      this.pressTimer = null;
      navigator.vibrate?.(10); // лёгкий отклик на Android, где поддерживается
      this.startTitleEdit();
    }, PlanPage.LONG_PRESS_MS);
  }

  /** Палец отпустили/увели раньше времени — это не долгое нажатие. */
  titlePressCancel(): void {
    if (this.pressTimer) clearTimeout(this.pressTimer);
    this.pressTimer = null;
  }

  startTitleEdit(): void {
    if (this.editingTitle() || !this.plan()) return;
    this.editingTitle.set(true);
    // contenteditable появится после отрисовки — тогда фокус и выделение всего текста.
    setTimeout(() => {
      const el = this.titleEl()?.nativeElement;
      if (!el) return;
      el.focus();
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
    });
  }

  /** Esc: возвращаем прежнее название без запроса. */
  cancelTitleEdit(): void {
    const el = this.titleEl()?.nativeElement;
    const p = this.plan();
    if (el && p) el.textContent = p.title;
    this.editingTitle.set(false);
    el?.blur();
  }

  /** Blur/Enter: сохраняем, если название изменилось и не пустое. */
  saveTitle(): void {
    if (!this.editingTitle()) return;
    this.editingTitle.set(false);
    const el = this.titleEl()?.nativeElement;
    const p = this.plan();
    if (!el || !p) return;
    const title = (el.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!title || title === p.title) {
      el.textContent = p.title; // пусто или без изменений — откатываем текст
      return;
    }
    // Оптимистично показываем новое название; при ошибке — откатываем.
    this.plan.set({ ...p, title });
    el.textContent = title;
    this.api.renamePlan(p.id, title).subscribe({
      next: (updated) => this.plan.set(updated),
      error: () => {
        this.plan.set(p);
        el.textContent = p.title;
      },
    });
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  // Суммарное время на готовку всего плана за раз
  cookTime(): string {
    const p = this.plan();
    if (!p) return '';
    return formatDuration(p.dishes.reduce((s, d) => s + d.prepMin + d.cookMin, 0));
  }

  pastel(i: number): string {
    return dishColorClass(i); // цвет блюда по индексу (единая палитра)
  }

  providerKey(provider: string): string {
    return providerToModel(provider); // provider → ключ модели для оценки
  }

  dishWord(n: number): string {
    const d10 = n % 10;
    const d100 = n % 100;
    if (d10 === 1 && d100 !== 11) return 'блюдо';
    if (d10 >= 2 && d10 <= 4 && (d100 < 12 || d100 > 14)) return 'блюда';
    return 'блюд';
  }

  statusLabel(s: PlanStatus): string {
    return s === 'accepted' ? '✓ Принят' : s === 'rejected' ? 'Отклонён' : 'Черновик';
  }

  setStatus(status: 'accepted' | 'rejected'): void {
    const p = this.plan();
    if (!p) return;
    this.api.setStatus(p.id, status).subscribe({ next: (u) => this.plan.set(u) });
  }

  continueChat(): void {
    const p = this.plan();
    if (p?.conversationId) {
      this.store.loadConversation(p.conversationId);
    }
    this.router.navigate(['/chat']);
  }
}
