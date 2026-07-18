import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CookingPlan, CookingStep, EasyWeekApi, PlanSummary } from '../../services/api';
import { Dish, WeekPlan } from '../../models/plan.model';
import { ChatStore } from '../../services/chat-store';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { PlanPicker } from '../../shared/plan-picker';

@Component({
  selector: 'ew-cooking-plan',
  imports: [RouterLink, CookingLoader, PlanPicker],
  templateUrl: './cooking.html',
  styleUrl: './cooking.scss',
})
export class CookingPlanPage {
  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);

  // /cooking/:planId — конкретный план; /cooking — выбранный «текущий» (или принятый/первый).
  readonly planId = input<string>('');

  readonly plan = signal<CookingPlan | null>(null);
  readonly weekPlan = signal<WeekPlan | null>(null); // блюда плана — для ссылок в рецепты
  readonly plans = signal<PlanSummary[]>([]);
  readonly selectedId = signal('');
  readonly loading = signal(true);
  readonly failed = signal(false);
  readonly errorMsg = signal('');
  readonly empty = signal(false);
  readonly title = signal('');
  readonly currentPlanId = signal('');
  readonly modelMenuOpen = signal(false);

  // Модели, для которых варианта плана готовки ещё нет (для ⟳). Пусто → ⟳ прячем.
  readonly remainingModels = computed<RecipeModel[]>(() => {
    const have = new Set(this.plan()?.variantModels ?? []);
    return ALL_MODELS.filter((m) => !have.has(m));
  });

  // Шаги, сгруппированные по фазам (в порядке order).
  readonly phases = computed<{ phase: string; steps: CookingStep[] }[]>(() => {
    const steps = [...(this.plan()?.steps ?? [])].sort((a, b) => a.order - b.order);
    const out: { phase: string; steps: CookingStep[] }[] = [];
    for (const s of steps) {
      const ph = s.phase || 'Готовка';
      let g = out[out.length - 1];
      if (!g || g.phase !== ph) {
        g = { phase: ph, steps: [] };
        out.push(g);
      }
      g.steps.push(s);
    }
    return out;
  });

  constructor() {
    effect(() => {
      const pid = this.planId();
      this.load(pid);
    });
  }

  private load(inputPlanId: string): void {
    this.loading.set(true);
    this.failed.set(false);
    this.empty.set(false);
    this.plan.set(null);
    this.api.listPlans().subscribe({
      next: (list) => {
        this.plans.set(list);
        if (!list.length) {
          this.loading.set(false);
          this.empty.set(true);
          return;
        }
        const resolve = (curId: string | null) => {
          const inList = (id: string) => list.some((p) => p.id === id);
          const target =
            (inputPlanId && inList(inputPlanId) && inputPlanId) ||
            (curId && inList(curId) && curId) ||
            list.find((p) => p.status === 'accepted')?.id ||
            list[0].id;
          this.selectedId.set(target);
          this.title.set(list.find((p) => p.id === target)?.title ?? '');
          this.loadWeekPlan(target);
          this.fetch(target);
        };
        // Явный план в URL важнее; иначе — выбранный «текущий» с сервера.
        if (inputPlanId) resolve(null);
        else
          this.api.getCurrentPlan().subscribe({
            next: (r) => resolve(r.planId),
            error: () => resolve(null),
          });
      },
      error: () => {
        this.loading.set(false);
        this.empty.set(true);
      },
    });
  }

  // Смена плана из селектора: сохраняем выбор на сервере (общий) и грузим его.
  onPickPlan(id: string): void {
    this.selectedId.set(id);
    this.title.set(this.plans().find((p) => p.id === id)?.title ?? '');
    this.api.setCurrentPlan(id).subscribe();
    this.loadWeekPlan(id);
    this.fetch(id);
  }

  private loadWeekPlan(id: string): void {
    this.api.getPlan(id).subscribe({
      next: (p) => this.weekPlan.set(p),
      error: () => this.weekPlan.set(null),
    });
  }

  // Блюда плана — для ссылок в рецепты.
  readonly dishes = computed<Dish[]>(() => this.weekPlan()?.dishes ?? []);

  // Модели-варианты рецепта, КРОМЕ модели активного плана готовки (дедуп для скобок).
  otherVariantLabels(dish: Dish): string[] {
    const planModel = this.plan()?.activeModel;
    return (dish.variantModels ?? [])
      .filter((m) => m !== planModel)
      .map((m) => this.modelLabel(m));
  }

  private fetch(planId: string, model?: string, action: 'open' | 'select' = 'open'): void {
    this.currentPlanId.set(planId);
    this.loading.set(true);
    this.failed.set(false);
    this.api.cookingPlan(planId, model ?? this.store.recipeModel(), action).subscribe({
      next: (cp) => {
        this.plan.set(cp);
        this.loading.set(false);
      },
      error: (err) => {
        this.errorMsg.set(err?.error?.detail ?? '');
        this.failed.set(true);
        this.loading.set(false);
      },
    });
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  chooseModel(model: string): void {
    this.modelMenuOpen.set(false);
    const p = this.plan();
    if (this.loading() || (p && model === p.activeModel)) return;
    this.fetch(this.currentPlanId(), model, 'select');
  }

  totalTime(step: CookingStep): number {
    return step.activeMin + step.passiveMin;
  }
}
