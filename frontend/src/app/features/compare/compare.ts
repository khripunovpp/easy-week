import { Location } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Router } from '@angular/router';
import { DishVariant, EasyWeekApi } from '../../services/api';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { ingTokens, levRatio } from '../../shared/ingredient-match';

// Порядок категорий (как в списке покупок) — по нему группируем/сортируем ингредиенты.
const CATEGORY_ORDER = [
  'Мясо и птица',
  'Рыба',
  'Овощи',
  'Молочное',
  'Бакалея',
  'Специи',
  'Прочее',
];

@Component({
  selector: 'ew-compare',
  imports: [CookingLoader],
  templateUrl: './compare.html',
  styleUrl: './compare.scss',
})
export class ComparePage {
  private readonly api = inject(EasyWeekApi);
  private readonly location = inject(Location);
  private readonly router = inject(Router);

  readonly planId = input<string>('');
  readonly dishId = input<string>('');

  readonly variants = signal<DishVariant[] | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);

  constructor() {
    effect(() => {
      const pid = this.planId();
      const did = this.dishId();
      if (!pid || !did) return;
      this.loading.set(true);
      this.failed.set(false);
      this.api.dishVariants(pid, did).subscribe({
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
    else this.router.navigate(['/plan', this.planId(), 'dish', this.dishId()]);
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  // --- Похожесть ингредиентов: взвешенный многослойный скор, порог >50% (общий матчер) ---
  private ingSimilarity(a: string, b: string): number {
    const ta = ingTokens(a);
    const tb = ingTokens(b);
    if (ta.length && ta.slice().sort().join(' ') === tb.slice().sort().join(' ')) return 1;
    const sa = [...new Set(ta)];
    const sb = [...new Set(tb)];
    let inter = 0;
    for (const t of sa) if (sb.some((u) => u === t || levRatio(t, u) >= 0.8)) inter++;
    const dice = sa.length + sb.length ? (2 * inter) / (sa.length + sb.length) : 0;
    const lev = levRatio(ta.join(' '), tb.join(' '));
    return 0.7 * dice + 0.3 * lev;
  }

  private readonly ING_SIM_THRESHOLD = 0.6;

  private catIndex(c: string): number {
    const i = CATEGORY_ORDER.indexOf(c);
    return i < 0 ? CATEGORY_ORDER.length : i;
  }

  // Выровненная таблица ингредиентов: строка = сматченный ингредиент, ячейка = количество у модели.
  // Сортировка: сначала по категории, потом по имени; firstCat помечает первую строку категории.
  readonly compareRows = computed(() => {
    const vs = this.variants();
    if (!vs || !vs.length) return [];
    const n = vs.length;
    const groups: { rep: string; category: string; cells: (string | null)[] }[] = [];
    vs.forEach((v, mi) => {
      for (const ing of v.ingredients) {
        let g = groups.find(
          (gr) => this.ingSimilarity(gr.rep, ing.name) >= this.ING_SIM_THRESHOLD,
        );
        if (!g) {
          g = { rep: ing.name, category: ing.category || 'Прочее', cells: Array(n).fill(null) };
          groups.push(g);
        }
        g.cells[mi] = `${ing.qty} ${ing.unit}`;
      }
    });
    const rows = groups
      .map((g) => {
        const present = g.cells.filter((c): c is string => c !== null);
        const diff = present.length < n || new Set(present).size > 1;
        return { name: g.rep, category: g.category, cells: g.cells, diff, firstCat: false };
      })
      .sort(
        (a, b) =>
          this.catIndex(a.category) - this.catIndex(b.category) ||
          a.name.localeCompare(b.name, 'ru'),
      );
    let prev = '';
    for (const r of rows) {
      r.firstCat = r.category !== prev;
      prev = r.category;
    }
    return rows;
  });

  // Шаги, выровненные по номеру: строка = «Шаг N», ячейка = текст шага у модели (или null).
  readonly stepRows = computed(() => {
    const vs = this.variants();
    if (!vs || !vs.length) return [];
    const max = Math.max(...vs.map((v) => v.steps.length));
    const rows: { n: number; cells: (string | null)[] }[] = [];
    for (let i = 0; i < max; i++) {
      rows.push({ n: i + 1, cells: vs.map((v) => v.steps[i] ?? null) });
    }
    return rows;
  });
}
