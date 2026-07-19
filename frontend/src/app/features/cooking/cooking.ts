import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CookingPlan, CookingStep, EasyWeekApi, PlanSummary } from '../../services/api';
import { Dish, WeekPlan } from '../../models/plan.model';
import { ChatStore } from '../../services/chat-store';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { ingTokens } from '../../shared/ingredient-match';
import { HlOwner, highlightStepText } from '../../shared/step-highlight';
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

  // Шаги, сгруппированные по фазам (в порядке order); html — подсветка ингредиентов
  // (только для мульти-блюдных шагов; иначе null → рендерим чистый текст).
  readonly phases = computed<{ phase: string; steps: { s: CookingStep; html: string | null }[] }[]>(
    () => {
      const steps = [...(this.plan()?.steps ?? [])].sort((a, b) => a.order - b.order);
      const out: { phase: string; steps: { s: CookingStep; html: string | null }[] }[] = [];
      for (const s of steps) {
        const ph = s.phase || 'Готовка';
        let g = out[out.length - 1];
        if (!g || g.phase !== ph) {
          g = { phase: ph, steps: [] };
          out.push(g);
        }
        g.steps.push({ s, html: this.stepHtml(s) });
      }
      return out;
    },
  );

  // Подсветка ингредиентов в тексте шага цветом блюда-владельца.
  // Гейт: только шаги с >1 блюдом (у одношаговых владелец один — не парсим).
  private stepHtml(s: CookingStep): string | null {
    if ((s.dishes?.length ?? 0) <= 1) return null;
    const owners: HlOwner[] = [];
    for (const name of s.dishes) {
      const cls = this.dishClassByName(name);
      const dish = this.dishByName(name);
      if (!cls || !dish) continue;
      for (const ing of dish.ingredients) {
        const toks = ingTokens(ing.name);
        if (!toks.length) continue;
        owners.push({ tokens: toks, dishId: dish.id, colorClass: cls }); // фраза целиком (длинные — вперёд)
        for (const t of toks)
          if (t.length > 2) owners.push({ tokens: [t], dishId: dish.id, colorClass: cls }); // головное слово в тексте
      }
    }
    return owners.length ? highlightStepText(s.text, owners, this.selectedDishIds()) : null;
  }

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
    this.selectedDishIds.set(new Set());
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
          // Прямая ссылка на конкретный план → делаем его текущим (как выбор в селекторе).
          if (inputPlanId && target === inputPlanId) this.api.setCurrentPlan(target).subscribe();
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
  // Даты текущего плана — в подзаголовок шапки.
  readonly currentWeekLabel = computed(
    () => this.plans().find((p) => p.id === this.selectedId())?.weekLabel ?? '',
  );

  private normName(s: string): string {
    return (s || '').trim().toLowerCase();
  }
  // Блюдо по имени из шага/чипа (точное, затем частичное совпадение).
  dishByName(name: string): Dish | undefined {
    const n = this.normName(name);
    const ds = this.dishes();
    return (
      ds.find((d) => this.normName(d.name) === n) ??
      ds.find((d) => this.normName(d.name).includes(n) || n.includes(this.normName(d.name)))
    );
  }
  dishColorClass(i: number): string {
    return dishColorClass(i);
  }

  // Фильтр по блюдам: выбранные рецепты подсвечивают свои шаги, остальные гаснут. Мультивыбор.
  readonly selectedDishIds = signal<ReadonlySet<string>>(new Set());
  toggleDish(id: string): void {
    this.selectedDishIds.update((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  isDishSelected(id: string): boolean {
    return this.selectedDishIds().has(id);
  }
  filterActive(): boolean {
    return this.selectedDishIds().size > 0;
  }
  // Шаг активен, если фильтр пуст или в шаге есть хотя бы одно выбранное блюдо.
  stepActive(s: CookingStep): boolean {
    const sel = this.selectedDishIds();
    if (!sel.size) return true;
    for (const name of s.dishes) {
      const d = this.dishByName(name);
      if (d && sel.has(d.id)) return true;
    }
    return false;
  }
  // Класс цвета по имени блюда (индекс в плане). Пусто, если блюдо не сопоставилось.
  dishClassByName(name: string): string {
    const d = this.dishByName(name);
    const i = d ? this.dishes().indexOf(d) : -1;
    return i >= 0 ? dishColorClass(i) : '';
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
