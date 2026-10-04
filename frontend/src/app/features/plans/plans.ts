import { NgTemplateOutlet } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { PlanStatus } from '../../models/plan.model';
import { EasyWeekApi, PlanSummary, RecipeItem } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { dayGroupLabel, dayKey, formatDuration } from '../../shared/format';
import { Modal } from '../../shared/modal';
import { RecipeAdd } from './recipe-add';

type PlansMode = 'plans' | 'recipes';
type RecipesGroup = 'plans' | 'dates' | 'favorites';
interface RecipeSection {
  id: string;
  label: string;
  items: RecipeItem[];
  showPlan: boolean; // в подписи строки — название плана (в группировке «Избранное»)
  hint?: string;
}

const MODE_KEY = 'ew.plansMode';
const GROUP_KEY = 'ew.recipesGroup';

function readPref<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key) as T | null;
    return v && allowed.includes(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

function writePref(key: string, v: string): void {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* приватный режим — просто не запомним */
  }
}

// Страница «Планы» с двумя режимами (сегмент вверху, выбор запоминается на устройстве):
// «Планы» — принятые + история; «Рецепты» — блюда только из ПРИНЯТЫХ планов (GET /api/recipes),
// у каждого звезда «избранное» (общая для семьи, по названию блюда — переживает правки плана).
// Рецепты группируются «По планам» (секция на план) или «Избранное» (★ избранные, затем
// остальные; одно блюдо — одна строка, открывается из самого свежего плана).
@Component({
  selector: 'ew-plans',
  imports: [RouterLink, CookingLoader, NgTemplateOutlet, Modal, RecipeAdd],
  templateUrl: './plans.html',
  styleUrl: './plans.scss',
})
export class Plans {
  private readonly api = inject(EasyWeekApi);
  private readonly router = inject(Router);

  // Модалка «Свой рецепт»: после «Дальше» рецепт уже в «Мои рецепты» — открываем его страницу.
  readonly addOpen = signal(false);
  onRecipeCreated(r: { planId: string; dishId: string }): void {
    this.addOpen.set(false);
    this.loadRecipes(true);
    this.router.navigate(['/plan', r.planId, 'dish', r.dishId]);
  }

  readonly plans = signal<PlanSummary[]>([]);
  readonly loading = signal(true);

  // Глобальный поиск: по названию плана и по названиям блюд внутри. Активен, если в поле
  // есть хоть один непробельный символ → показываем плоский список результатов (без групп).
  readonly query = signal('');
  readonly searchActive = computed(() => this.query().trim().length > 0);
  readonly results = computed<{ plan: PlanSummary; dishes: string[] }[]>(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return [];
    const out: { plan: PlanSummary; dishes: string[] }[] = [];
    for (const p of this.plans()) {
      const dishes = (p.dishNames ?? []).filter((n) => n.toLowerCase().includes(q));
      if (p.title.toLowerCase().includes(q) || dishes.length) out.push({ plan: p, dishes });
    }
    return out;
  });
  onSearch(v: string): void {
    this.query.set(v);
  }

  // Сверху — принятые; ниже — История (отклонённые + черновики).
  // Список с бэка отсортирован по дате (свежие первыми), поэтому slice(0, N) = последние.
  private readonly accepted = computed(() => this.plans().filter((p) => p.status === 'accepted'));
  private readonly history = computed(() => this.plans().filter((p) => p.status !== 'accepted'));

  readonly acceptedShown = signal(3);
  readonly historyShown = signal(3);

  readonly acceptedList = computed(() => this.accepted().slice(0, this.acceptedShown()));
  readonly historyList = computed(() => this.history().slice(0, this.historyShown()));
  readonly moreAccepted = computed(() => this.accepted().length - this.acceptedShown());
  readonly moreHistory = computed(() => this.history().length - this.historyShown());

  showMoreAccepted(): void {
    this.acceptedShown.update((n) => n + 10);
  }
  showMoreHistory(): void {
    this.historyShown.update((n) => n + 10);
  }

  // Удаление с подтверждением (действие необратимо)
  readonly pendingDelete = signal<PlanSummary | null>(null);
  readonly deleting = signal(false);

  askDelete(plan: PlanSummary): void {
    this.pendingDelete.set(plan);
  }
  cancelDelete(): void {
    this.pendingDelete.set(null);
  }
  confirmDelete(): void {
    const plan = this.pendingDelete();
    if (!plan || this.deleting()) return;
    this.deleting.set(true);
    this.api.deletePlan(plan.id).subscribe({
      next: () => {
        this.plans.update((list) => list.filter((p) => p.id !== plan.id));
        this.pendingDelete.set(null);
        this.deleting.set(false);
      },
      error: () => this.deleting.set(false),
    });
  }

  // ---- Режим «Рецепты» ----
  readonly mode = signal<PlansMode>(readPref(MODE_KEY, ['plans', 'recipes'] as const, 'plans'));
  readonly group = signal<RecipesGroup>(readPref(GROUP_KEY, ['plans', 'dates', 'favorites'] as const, 'plans'));
  readonly recipes = signal<RecipeItem[]>([]);
  readonly recipesLoading = signal(false);
  readonly recipesError = signal(false);
  private recipesLoaded = false;

  setMode(m: PlansMode): void {
    this.mode.set(m);
    writePref(MODE_KEY, m);
    if (m === 'recipes') this.loadRecipes();
  }

  setGroup(g: RecipesGroup): void {
    this.group.set(g);
    writePref(GROUP_KEY, g);
  }

  loadRecipes(force = false): void {
    if ((this.recipesLoaded && !force) || this.recipesLoading()) return;
    this.recipesLoading.set(true);
    this.recipesError.set(false);
    this.api.listRecipes().subscribe({
      next: (list) => {
        this.recipes.set(list);
        this.recipesLoaded = true;
        this.recipesLoading.set(false);
      },
      error: () => {
        this.recipesError.set(true);
        this.recipesLoading.set(false);
      },
    });
  }

  // Поиск в режиме рецептов — по названию блюда и тегам.
  private readonly recipesFiltered = computed(() => {
    const q = this.query().trim().toLowerCase();
    const list = this.recipes();
    if (!q) return list;
    return list.filter(
      (r) => r.name.toLowerCase().includes(q) || r.tags.some((t) => t.toLowerCase().includes(q)),
    );
  });

  readonly recipeSections = computed<RecipeSection[]>(() => {
    const list = this.recipesFiltered();
    if (this.group() === 'plans') {
      const byPlan = new Map<string, RecipeSection>();
      for (const r of list) {
        let sec = byPlan.get(r.planId);
        if (!sec) {
          // У «Мои рецепты» недели нет — только название.
          const label = r.weekLabel ? `${r.planTitle} · ${r.weekLabel}` : r.planTitle;
          sec = { id: r.planId, label, items: [], showPlan: false };
          byPlan.set(r.planId, sec);
        }
        sec.items.push(r);
      }
      return [...byPlan.values()];
    }
    if (this.group() === 'dates') return this.dateSections(list);
    // «Избранное»: одно блюдо — одна строка (первое вхождение = самый свежий план).
    const seen = new Set<string>();
    const fav: RecipeItem[] = [];
    const rest: RecipeItem[] = [];
    for (const r of list) {
      if (seen.has(r.key)) continue;
      seen.add(r.key);
      (r.favorite ? fav : rest).push(r);
    }
    const out: RecipeSection[] = [
      {
        id: 'fav',
        label: '★ Избранное',
        items: fav,
        showPlan: true,
        hint: fav.length ? undefined : 'Отмечайте ☆ любимые рецепты — они соберутся здесь.',
      },
    ];
    if (rest.length) out.push({ id: 'rest', label: 'Остальные', items: rest, showPlan: true });
    // При поиске пустую секцию избранного не показываем — только найденное.
    return this.query().trim() ? out.filter((s) => s.items.length) : out;
  });

  // «По дате»: по дню генерации рецепта (нет рецепта — день принятия плана), свежие сверху:
  // «Сегодня», «Вчера», дальше даты. Одно блюдо — одна строка (самое свежее), в подписи — план.
  private dateSections(list: RecipeItem[]): RecipeSection[] {
    const when = (r: RecipeItem) => r.generatedAt || r.planDecidedAt || '';
    const sorted = [...list].sort((a, b) => (when(b) > when(a) ? 1 : when(b) < when(a) ? -1 : 0));
    const seen = new Set<string>();
    const byDay = new Map<string, RecipeSection>();
    for (const r of sorted) {
      if (seen.has(r.key)) continue;
      seen.add(r.key);
      const key = dayKey(when(r));
      let sec = byDay.get(key);
      if (!sec) {
        sec = { id: `day-${key || 'none'}`, label: dayGroupLabel(when(r)) || 'Без даты', items: [], showPlan: true };
        byDay.set(key, sec);
      }
      sec.items.push(r);
    }
    return [...byDay.values()];
  }

  // Звезда: оптимистично для всех вхождений блюда (ключ — название), ошибка — откат.
  toggleFavorite(r: RecipeItem): void {
    const next = !r.favorite;
    const apply = (on: boolean) =>
      this.recipes.update((list) => list.map((x) => (x.key === r.key ? { ...x, favorite: on } : x)));
    apply(next);
    this.api.setFavorite(r, next).subscribe({ error: () => apply(!next) });
  }

  recipeTime(r: RecipeItem): string {
    const total = (r.prepMin || 0) + (r.cookMin || 0);
    return total ? formatDuration(total) : '';
  }

  constructor() {
    if (this.mode() === 'recipes') this.loadRecipes();
    this.api.listPlans().subscribe({
      next: (plans) => {
        this.plans.set(plans);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  statusLabel(s: PlanStatus): string {
    return s === 'accepted' ? 'Принят' : s === 'rejected' ? 'Отклонён' : 'Черновик';
  }

  cookTime(mins: number): string {
    return formatDuration(mins);
  }

  pastel(i: number): string {
    return dishColorClass(i); // цвет по индексу (единая палитра)
  }
}
