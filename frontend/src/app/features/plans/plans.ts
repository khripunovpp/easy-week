import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PlanStatus } from '../../models/plan.model';
import { EasyWeekApi, PlanSummary } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';
import { formatDuration } from '../../shared/format';

@Component({
  selector: 'ew-plans',
  imports: [RouterLink, CookingLoader],
  templateUrl: './plans.html',
  styleUrl: './plans.scss',
})
export class Plans {
  private readonly api = inject(EasyWeekApi);

  readonly plans = signal<PlanSummary[]>([]);
  readonly loading = signal(true);

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
}
