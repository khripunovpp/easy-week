import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CookingPlan, CookingStep, EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-cooking-plan',
  imports: [RouterLink, CookingLoader],
  templateUrl: './cooking.html',
  styleUrl: './cooking.scss',
})
export class CookingPlanPage {
  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);

  // /cooking/:planId — конкретный план; /cooking — последний (принятый в приоритете).
  readonly planId = input<string>('');

  readonly plan = signal<CookingPlan | null>(null);
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

  private load(planId: string): void {
    this.loading.set(true);
    this.failed.set(false);
    this.empty.set(false);
    this.plan.set(null);
    if (planId) {
      this.fetch(planId);
      return;
    }
    // Без явного плана — берём последний (принятый в приоритете), как в покупках.
    this.api.listPlans().subscribe({
      next: (plans) => {
        if (!plans.length) {
          this.loading.set(false);
          this.empty.set(true);
          return;
        }
        const target = plans.find((p) => p.status === 'accepted') ?? plans[0];
        this.title.set(target.title);
        this.fetch(target.id);
      },
      error: () => {
        this.loading.set(false);
        this.empty.set(true);
      },
    });
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
