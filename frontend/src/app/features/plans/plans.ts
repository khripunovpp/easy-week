import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MOCK_HISTORY } from '../../data/mock-plan';
import { PlanStatus, WeekPlan } from '../../models/plan.model';

@Component({
  selector: 'ew-plans',
  imports: [RouterLink],
  templateUrl: './plans.html',
  styleUrl: './plans.scss',
})
export class Plans {
  readonly plans = MOCK_HISTORY;

  readonly drafts = this.plans.filter((p) => p.status === 'draft');
  readonly decided = this.plans.filter((p) => p.status !== 'draft');

  statusLabel(s: PlanStatus): string {
    return s === 'accepted' ? 'Принят' : s === 'rejected' ? 'Отклонён' : 'Черновик';
  }

  emoji(plan: WeekPlan): string {
    return plan.dishes[0]?.emoji ?? '🍽️';
  }
}
