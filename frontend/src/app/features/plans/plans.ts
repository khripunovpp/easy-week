import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PlanStatus } from '../../models/plan.model';
import { EasyWeekApi, PlanSummary } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';

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

  readonly drafts = computed(() => this.plans().filter((p) => p.status === 'draft'));
  readonly decided = computed(() => this.plans().filter((p) => p.status !== 'draft'));

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
}
