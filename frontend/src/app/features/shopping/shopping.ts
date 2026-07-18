import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { EasyWeekApi, PlanSummary, ShoppingGroup, ShoppingListItem } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';
import { PlanPicker } from '../../shared/plan-picker';

// Порядок категорий в списке (как на бэке). Незнакомые — в конце.
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
  selector: 'ew-shopping',
  imports: [CookingLoader, PlanPicker],
  templateUrl: './shopping.html',
  styleUrl: './shopping.scss',
})
export class Shopping {
  private readonly api = inject(EasyWeekApi);

  // /shopping/:planId — конкретный план; /shopping — выбранный «текущий» (или принятый/первый).
  readonly planId = input<string>('');

  readonly items = signal<ShoppingListItem[]>([]);
  readonly plans = signal<PlanSummary[]>([]);
  readonly selectedId = signal('');
  readonly loading = signal(true);
  readonly empty = signal(false);
  readonly title = signal('');
  readonly currentPlanId = signal('');

  private readonly checked = signal<Set<string>>(new Set());
  private activePlanId = '';

  // Живая группировка по категориям. Отмеченные (купленные) уезжают в конец
  // своей категории, невыбранные — сверху; внутри — по алфавиту.
  readonly groups = computed<ShoppingGroup[]>(() => {
    const checked = this.checked();
    const byCat = new Map<string, ShoppingListItem[]>();
    for (const it of this.items()) {
      const cat = it.category || 'Прочее';
      (byCat.get(cat) ?? byCat.set(cat, []).get(cat)!).push(it);
    }
    const order = [
      ...CATEGORY_ORDER.filter((c) => byCat.has(c)),
      ...[...byCat.keys()].filter((c) => !CATEGORY_ORDER.includes(c)),
    ];
    return order.map((category) => ({
      category,
      items: [...byCat.get(category)!].sort((a, b) => {
        const da = checked.has(this.key(a)) ? 1 : 0;
        const db = checked.has(this.key(b)) ? 1 : 0;
        if (da !== db) return da - db; // невыбранные сверху, отмеченные — вниз
        return a.name.localeCompare(b.name, 'ru');
      }),
    }));
  });

  readonly total = computed(() => this.items().length);
  readonly doneCount = computed(() => this.checked().size);

  constructor() {
    effect(() => {
      const pid = this.planId();
      this.load(pid);
    });
  }

  private load(inputPlanId: string): void {
    this.loading.set(true);
    this.empty.set(false);
    this.items.set([]);
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
          this.fetch(target);
        };
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
    this.fetch(id);
  }

  private fetch(planId: string): void {
    this.activePlanId = planId;
    this.currentPlanId.set(planId);
    this.checked.set(this.loadChecked(planId));

    // Мгновенно показываем закэшированный список (в т.ч. офлайн), затем обновляем с сервера.
    const cached = this.loadItems(planId);
    this.items.set(cached);
    if (cached.length) this.loading.set(false);

    this.api.shoppingList(planId).subscribe({
      next: (groups) => {
        const items = groups.flatMap((g) => g.items);
        this.items.set(items);
        this.saveItems(planId, items);
        this.loading.set(false);
        this.empty.set(items.length === 0);
      },
      error: () => {
        // Офлайн/ошибка — остаёмся на кэше, если он есть.
        this.loading.set(false);
        this.empty.set(this.items().length === 0);
      },
    });
  }

  key(item: { name: string; unit: string }): string {
    return `${item.name.toLowerCase()}__${item.unit}`;
  }

  isChecked(item: { name: string; unit: string }): boolean {
    return this.checked().has(this.key(item));
  }

  toggle(item: { name: string; unit: string }): void {
    const k = this.key(item);
    this.checked.update((set) => {
      const next = new Set(set);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });
    this.saveChecked();
  }

  fmtQty(item: { qty: number; unit: string }): string {
    return `${item.qty} ${item.unit}`;
  }

  private storageKey(planId: string): string {
    return `ew-shopping-${planId}`;
  }

  // Кэш самих позиций списка (для мгновенного показа и офлайна).
  private itemsKey(planId: string): string {
    return `ew-shopping-items-${planId}`;
  }
  private loadItems(planId: string): ShoppingListItem[] {
    try {
      const raw = localStorage.getItem(this.itemsKey(planId));
      return raw ? (JSON.parse(raw) as ShoppingListItem[]) : [];
    } catch {
      return [];
    }
  }
  private saveItems(planId: string, items: ShoppingListItem[]): void {
    try {
      localStorage.setItem(this.itemsKey(planId), JSON.stringify(items));
    } catch {
      /* localStorage может быть недоступен — не критично */
    }
  }

  private loadChecked(planId: string): Set<string> {
    try {
      const raw = localStorage.getItem(this.storageKey(planId));
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  }

  private saveChecked(): void {
    if (!this.activePlanId) return;
    localStorage.setItem(
      this.storageKey(this.activePlanId),
      JSON.stringify([...this.checked()]),
    );
  }
}
