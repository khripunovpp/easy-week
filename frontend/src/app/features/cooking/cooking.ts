import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { CookingPlan, CookingStep, EasyWeekApi, PlanSummary } from '../../services/api';
import { Dish, WeekPlan } from '../../models/plan.model';
import { ChatStore } from '../../services/chat-store';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { ModelSettings } from '../../services/model-settings';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { ingTokens } from '../../shared/ingredient-match';
import { HlOwner, highlightStepText } from '../../shared/step-highlight';
import { PlanPicker } from '../../shared/plan-picker';
import { Vote } from '../../shared/vote';
import { formatGeneratedAt } from '../../shared/format';
import { ModelName } from '../../shared/model-name';

@Component({
  selector: 'ew-cooking-plan',
  imports: [RouterLink, CookingLoader, PlanPicker, Vote, ModelName],
  templateUrl: './cooking.html',
  styleUrl: './cooking.scss',
})
export class CookingPlanPage {
  /** Подпись даты генерации: «сегодня, 14:05» / «26 сен, 14:05»; пусто — не показываем. */
  genAt(iso: string | null | undefined): string {
    return formatGeneratedAt(iso);
  }

  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly router = inject(Router);

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
  readonly generatingModel = signal<string | null>(null); // модель, чей вариант сейчас собирается
  // «↻ Перегенерировать»: пересборка идёт (старый план на экране) / ошибка последней попытки.
  readonly regenerating = signal(false);
  readonly regenError = signal('');
  readonly busy = computed(() => this.regenerating() || this.loading());
  // Футер действий — только когда есть собранный план готовки.
  readonly hasFooter = computed(
    () => !this.loading() && !this.empty() && !this.failed() && !!this.plan()?.steps?.length,
  );

  private readonly modelSettings = inject(ModelSettings);
  // Модели, для которых варианта плана готовки ещё нет (для ⟳). Пусто → ⟳ прячем.
  // Только годные для плана готовки (карта задач с сервера).
  readonly remainingModels = computed<RecipeModel[]>(() => {
    const have = new Set(this.plan()?.variantModels ?? []);
    return this.modelSettings.modelsFor('cooking').filter((m) => !have.has(m));
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
        // Явный план в URL — только если это актуальная версия (из списка); ссылка на
        // заменённую правкой версию → берём «текущий» с сервера (он уже указывает на новую).
        if (inputPlanId && list.some((p) => p.id === inputPlanId)) resolve(null);
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
    // Без явной модели — пусто: первый вариант соберёт настройка «План готовки» (на сервере).
    this.api.cookingPlan(planId, model ?? '', action).subscribe({
      next: (cp) => {
        this.plan.set(cp);
        this.loading.set(false);
        this.generatingModel.set(null);
        this.modelMenuOpen.set(false);
      },
      error: (err) => {
        this.errorMsg.set(err?.error?.detail ?? '');
        this.failed.set(true);
        this.loading.set(false);
        this.generatingModel.set(null);
        this.modelMenuOpen.set(false);
      },
    });
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  // Выбор модели в выпадашке: активная — закрыть; несгенерённую держим открытой со спиннером.
  chooseModel(model: string): void {
    const p = this.plan();
    if (!p || model === p.activeModel) {
      this.modelMenuOpen.set(false);
      return;
    }
    if (this.loading() || this.regenerating()) return;
    const isNew = !(p.variantModels ?? []).includes(model);
    if (isNew) this.generatingModel.set(model);
    else this.modelMenuOpen.set(false);
    this.fetch(this.currentPlanId(), model, 'select');
  }

  totalTime(step: CookingStep): number {
    return step.activeMin + step.passiveMin;
  }

  // «↻ Перегенерировать»: принудительно пересобрать план готовки текущей моделью с учётом
  // обсуждения плана в чате. При ошибке старый план остаётся — показываем ошибку в футере.
  regenerate(): void {
    const cp = this.plan();
    const pid = this.currentPlanId();
    if (!cp || !pid || this.busy()) return;
    this.regenerating.set(true);
    this.regenError.set('');
    this.modelMenuOpen.set(false);
    // Пусто (варианта ещё нет) → бэк возьмёт настройку «План готовки».
    this.api.cookingPlan(pid, cp.activeModel ?? '', 'regenerate').subscribe({
      next: (np) => {
        this.plan.set(np);
        this.regenerating.set(false);
      },
      error: (err) => {
        this.regenError.set(err?.error?.detail ?? 'Не удалось пересобрать план готовки.');
        this.regenerating.set(false);
      },
    });
  }

  // «💬 Обсудить в чате»: беседа плана + бейдж «Обсуждение: план готовки».
  discuss(): void {
    const wp = this.weekPlan();
    if (!wp?.conversationId || this.busy()) return;
    this.store.startDiscuss({
      conversationId: wp.conversationId,
      target: 'cooking',
      planId: wp.id,
      name: 'план готовки',
      model: this.plan()?.activeModel,
    });
    this.router.navigate(['/chat']);
  }
}
