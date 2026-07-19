import { NgTemplateOutlet } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PlanStatus } from '../../models/plan.model';
import { EasyWeekApi, PlanSummary } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { formatDuration } from '../../shared/format';

@Component({
  selector: 'ew-plans',
  imports: [RouterLink, CookingLoader, NgTemplateOutlet],
  templateUrl: './plans.html',
  styleUrl: './plans.scss',
})
export class Plans {
  private readonly api = inject(EasyWeekApi);

  readonly plans = signal<PlanSummary[]>([]);
  readonly loading = signal(true);

  // Глобальный поиск: по названию плана и по названиям блюд внутри. Активен, если в поле
  // есть хоть один непробельный символ → показываем плоский список результатов (без групп).
  readonly query = signal('');
  readonly searchActive = computed(() => this.query().trim().length > 0);
  readonly results = computed(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return [];
    return this.plans().filter(
      (p) =>
        p.title.toLowerCase().includes(q) ||
        (p.dishNames ?? []).some((n) => n.toLowerCase().includes(q)),
    );
  });
  onSearch(v: string): void {
    this.query.set(v);
  }

  // Сверху — принятые; ниже — История (отклонённые + черновики).
  // Список с бэка отсортирован по дате (свежие первыми), поэтому slice(0, N) = последние.
  private readonly accepted = computed(() => this.plans().filter((p) => p.status === 'accepted'));
  private readonly history = computed(() => this.plans().filter((p) => p.status !== 'accepted'));

  readonly acceptedShown = signal(3);
  readonly historyShown = signal(3);

  readonly acceptedList = computed(() => this.accepted().slice(0, this.acceptedShown()));
  readonly historyList = computed(() => this.history().slice(0, this.historyShown()));
  readonly moreAccepted = computed(() => this.accepted().length - this.acceptedShown());
  readonly moreHistory = computed(() => this.history().length - this.historyShown());

  showMoreAccepted(): void {
    this.acceptedShown.update((n) => n + 10);
  }
  showMoreHistory(): void {
    this.historyShown.update((n) => n + 10);
  }

  // Удаление с подтверждением (действие необратимо)
  readonly pendingDelete = signal<PlanSummary | null>(null);
  readonly deleting = signal(false);

  askDelete(plan: PlanSummary): void {
    this.pendingDelete.set(plan);
  }
  cancelDelete(): void {
    this.pendingDelete.set(null);
  }
  confirmDelete(): void {
    const plan = this.pendingDelete();
    if (!plan || this.deleting()) return;
    this.deleting.set(true);
    this.api.deletePlan(plan.id).subscribe({
      next: () => {
        this.plans.update((list) => list.filter((p) => p.id !== plan.id));
        this.pendingDelete.set(null);
        this.deleting.set(false);
      },
      error: () => this.deleting.set(false),
    });
  }

  constructor() {
    this.api.listPlans().subscribe({
      next: (plans) => {
        this.plans.set(plans);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  statusLabel(s: PlanStatus): string {
    return s === 'accepted' ? 'Принят' : s === 'rejected' ? 'Отклонён' : 'Черновик';
  }

  cookTime(mins: number): string {
    return formatDuration(mins);
  }

  pastel(i: number): string {
    return dishColorClass(i); // цвет по индексу (единая палитра)
  }
}
