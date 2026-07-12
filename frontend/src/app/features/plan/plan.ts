import { Component, effect, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { PlanStatus, WeekPlan } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-plan',
  imports: [RouterLink, CookingLoader],
  templateUrl: './plan.html',
  styleUrl: './plan.scss',
})
export class PlanPage {
  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly router = inject(Router);

  readonly id = input<string>('');

  readonly plan = signal<WeekPlan | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);

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

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  pastel(i: number): string {
    return `pastel-${i % 5}`;
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
