import { Component, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Dish } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';

@Component({
  selector: 'ew-dish',
  imports: [RouterLink],
  templateUrl: './dish.html',
  styleUrl: './dish.scss',
})
export class DishPage {
  private readonly api = inject(EasyWeekApi);

  readonly planId = input<string>('');
  readonly dishId = input<string>('');

  readonly dish = signal<Dish | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);

  constructor() {
    // Догружаем блюдо (с ленивой генерацией шагов на бэкенде) при смене маршрута.
    effect(() => {
      const pid = this.planId();
      const did = this.dishId();
      if (!pid || !did) return;
      this.loading.set(true);
      this.failed.set(false);
      this.api.dishDetails(pid, did).subscribe({
        next: (d) => {
          this.dish.set(d);
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
}
