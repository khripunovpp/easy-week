import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { WeekPlan } from '../../models/plan.model';
import { EasyWeekApi, ShoppingGroup } from '../../services/api';

@Component({
  selector: 'ew-print',
  imports: [RouterLink],
  templateUrl: './print.html',
  styleUrl: './print.scss',
})
export class PrintPage {
  private readonly api = inject(EasyWeekApi);

  readonly planId = input<string>('');
  readonly parts = input<string>('all'); // recipes | shopping | all (query-параметр)

  readonly plan = signal<WeekPlan | null>(null);
  readonly shopping = signal<ShoppingGroup[]>([]);
  readonly loading = signal(true);

  readonly showRecipes = signal(true);
  readonly showShopping = signal(true);

  readonly ready = computed(() => !this.loading() && this.plan() !== null);

  constructor() {
    effect(() => {
      const pid = this.planId();
      const parts = this.parts();
      if (!pid) return;
      this.showRecipes.set(parts !== 'shopping');
      this.showShopping.set(parts !== 'recipes');
      this.loadAll(pid);
    });
  }

  private loadAll(planId: string): void {
    this.loading.set(true);
    this.api.fullPlan(planId).subscribe({
      next: (plan) => {
        this.plan.set(plan);
        this.api.shoppingList(planId).subscribe({
          next: (g) => {
            this.shopping.set(g);
            this.loading.set(false);
          },
          error: () => this.loading.set(false),
        });
      },
      error: () => this.loading.set(false),
    });
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  print(): void {
    window.print();
  }
}
