import { Location } from '@angular/common';
import { Component, effect, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dish } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-dish',
  imports: [RouterLink, CookingLoader],
  templateUrl: './dish.html',
  styleUrl: './dish.scss',
})
export class DishPage {
  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly location = inject(Location);
  private readonly router = inject(Router);

  readonly planId = input<string>('');
  readonly dishId = input<string>('');

  // Назад — туда, откуда пришли (план или чат). Если истории нет — на план.
  back(): void {
    if (history.length > 1) {
      this.location.back();
    } else {
      this.router.navigate(['/plan', this.planId()]);
    }
  }

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
      this.api.dishDetails(pid, did, this.store.recipeModel()).subscribe({
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
