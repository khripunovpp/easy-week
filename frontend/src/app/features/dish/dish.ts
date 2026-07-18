import { Location } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dish } from '../../models/plan.model';
import { DishVariant, EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
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
  readonly errorMsg = signal('');
  readonly modelMenuOpen = signal(false);

  // Модели, для которых варианта рецепта ещё нет (для ⟳-выпадашки). Пусто → ⟳ прячем.
  readonly remainingModels = computed<RecipeModel[]>(() => {
    const have = new Set(this.dish()?.variantModels ?? []);
    return ALL_MODELS.filter((m) => !have.has(m));
  });

  constructor() {
    // Догружаем блюдо (с ленивой генерацией шагов на бэкенде) при смене маршрута.
    effect(() => {
      const pid = this.planId();
      const did = this.dishId();
      if (!pid || !did) return;
      this.load(pid, did, this.store.recipeModel(), 'open');
    });
  }

  private load(pid: string, did: string, model: string, action: 'open' | 'select'): void {
    this.loading.set(true);
    this.failed.set(false);
    this.api.dishDetails(pid, did, model, action).subscribe({
      next: (d) => {
        this.dish.set(d);
        this.loading.set(false);
      },
      error: (err) => {
        // 429 (дневной лимит) и прочие ошибки — показываем текст с бэка, если есть
        this.errorMsg.set(err?.error?.detail ?? '');
        this.failed.set(true);
        this.loading.set(false);
      },
    });
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  // --- Сравнение вариантов рецепта по моделям ---
  readonly compareVariants = signal<DishVariant[] | null>(null);
  readonly comparing = signal(false);

  // --- Похожесть ингредиентов: взвешенный многослойный скор, порог >50% ---
  // Стоп-слова (предлоги/союзы) не считаем значимыми токенами.
  private readonly ingStop = new Set(['и', 'из', 'с', 'со', 'в', 'во', 'на', 'для', 'до', 'по']);

  // Значимые токены имени: нижний регистр, без скобок-уточнений «(среднего размера)»,
  // без пунктуации, без коротких слов и стоп-слов.
  private ingTokens(name: string): string[] {
    return (name || '')
      .toLowerCase()
      .replace(/\([^)]*\)/g, ' ')
      .replace(/[^\p{L}\p{N}\s]/gu, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .split(' ')
      .filter((t) => t.length > 1 && !this.ingStop.has(t));
  }

  // Символьная близость строк (Левенштейн → доля совпадения 0..1) — ловит опечатки/окончания.
  private levRatio(a: string, b: string): number {
    const m = a.length;
    const n = b.length;
    if (!m || !n) return m === n ? 1 : 0;
    const d = Array.from({ length: n + 1 }, (_, i) => i);
    for (let i = 1; i <= m; i++) {
      let prev = d[0];
      d[0] = i;
      for (let j = 1; j <= n; j++) {
        const tmp = d[j];
        d[j] = Math.min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
        prev = tmp;
      }
    }
    return 1 - d[n] / Math.max(m, n);
  }

  // Взвешенный скор похожести двух названий: совпадение токенов (Dice) + символьная близость.
  private ingSimilarity(a: string, b: string): number {
    const ta = this.ingTokens(a);
    const tb = this.ingTokens(b);
    if (ta.length && ta.slice().sort().join(' ') === tb.slice().sort().join(' ')) return 1;
    const sa = [...new Set(ta)];
    const sb = [...new Set(tb)];
    // Токены матчатся фаззи (Левенштейн ≥0.8) — чтобы «помидоры»≈«помидор», но не «говяжий»/«свиной».
    let inter = 0;
    for (const t of sa) if (sb.some((u) => u === t || this.levRatio(t, u) >= 0.8)) inter++;
    const dice = sa.length + sb.length ? (2 * inter) / (sa.length + sb.length) : 0;
    const lev = this.levRatio(ta.join(' '), tb.join(' '));
    return 0.7 * dice + 0.3 * lev; // токены важнее посимвольной близости
  }

  private readonly ING_SIM_THRESHOLD = 0.6; // > 50% с запасом, чтобы не склеивать «говяжий/свиной»

  // Выровненная таблица ингредиентов: строка = сматченный по вариантам ингредиент,
  // ячейка = количество у модели (или null, если у неё его нет). diff — если ингредиент
  // есть не у всех моделей ИЛИ количество расходится.
  readonly compareRows = computed(() => {
    const vs = this.compareVariants();
    if (!vs || !vs.length) return [];
    const n = vs.length;
    const groups: { rep: string; cells: (string | null)[] }[] = [];
    vs.forEach((v, mi) => {
      for (const ing of v.ingredients) {
        let g = groups.find(
          (gr) => this.ingSimilarity(gr.rep, ing.name) >= this.ING_SIM_THRESHOLD,
        );
        if (!g) {
          g = { rep: ing.name, cells: Array(n).fill(null) };
          groups.push(g);
        }
        g.cells[mi] = `${ing.qty} ${ing.unit}`;
      }
    });
    return groups
      .map((g) => {
        const present = g.cells.filter((c): c is string => c !== null);
        const diff = present.length < n || new Set(present).size > 1;
        return { name: g.rep, cells: g.cells, diff };
      })
      .sort((a, b) => a.name.localeCompare(b.name, 'ru'));
  });

  // Шаги, выровненные по номеру: строка = «Шаг N», ячейка = текст шага у модели (или null).
  readonly stepRows = computed(() => {
    const vs = this.compareVariants();
    if (!vs || !vs.length) return [];
    const max = Math.max(...vs.map((v) => v.steps.length));
    const rows: { n: number; cells: (string | null)[] }[] = [];
    for (let i = 0; i < max; i++) {
      rows.push({ n: i + 1, cells: vs.map((v) => v.steps[i] ?? null) });
    }
    return rows;
  });

  openCompare(): void {
    if (this.comparing()) return;
    this.comparing.set(true);
    this.api.dishVariants(this.planId(), this.dishId()).subscribe({
      next: (vs) => {
        this.compareVariants.set(vs);
        this.comparing.set(false);
      },
      error: () => this.comparing.set(false),
    });
  }

  closeCompare(): void {
    this.compareVariants.set(null);
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  // Клик по вкладке модели или выбор в ⟳-выпадашке: делаем вариант активным
  // (генерим на бэке, если его ещё нет). Активная модель — no-op.
  chooseModel(model: string): void {
    this.modelMenuOpen.set(false);
    const d = this.dish();
    if (!d || this.loading() || model === d.activeModel) return;
    this.load(this.planId(), this.dishId(), model, 'select');
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }
}
