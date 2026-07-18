import { Location } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Router } from '@angular/router';
import { CookingPlanVariant, CookingStep, EasyWeekApi } from '../../services/api';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-cooking-compare',
  imports: [CookingLoader],
  templateUrl: './cooking-compare.html',
  styleUrl: './cooking-compare.scss',
})
export class CookingComparePage {
  private readonly api = inject(EasyWeekApi);
  private readonly location = inject(Location);
  private readonly router = inject(Router);

  readonly planId = input<string>('');

  readonly variants = signal<CookingPlanVariant[] | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);

  constructor() {
    effect(() => {
      const pid = this.planId();
      if (!pid) return;
      this.loading.set(true);
      this.failed.set(false);
      this.api.cookingVariants(pid).subscribe({
        next: (vs) => {
          this.variants.set(vs);
          this.loading.set(false);
        },
        error: () => {
          this.failed.set(true);
          this.loading.set(false);
        },
      });
    });
  }

  back(): void {
    if (history.length > 1) this.location.back();
    else this.router.navigate(['/cooking', this.planId()]);
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  // Шаги, выровненные по номеру (order): строка = «Шаг N», ячейка = шаг у модели (или null).
  readonly stepRows = computed<{ order: number; cells: (CookingStep | null)[] }[]>(() => {
    const vs = this.variants();
    if (!vs || !vs.length) return [];
    const maxOrder = Math.max(0, ...vs.flatMap((v) => v.steps.map((s) => s.order)));
    const rows: { order: number; cells: (CookingStep | null)[] }[] = [];
    for (let o = 1; o <= maxOrder; o++) {
      rows.push({ order: o, cells: vs.map((v) => v.steps.find((s) => s.order === o) ?? null) });
    }
    return rows;
  });
}
