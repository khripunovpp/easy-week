import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { EasyWeekApi, ShoppingGroup, ShoppingListItem } from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';

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
  imports: [RouterLink, CookingLoader],
  templateUrl: './shopping.html',
  styleUrl: './shopping.scss',
})
export class Shopping {
  private readonly api = inject(EasyWeekApi);

  // /shopping/:planId — конкретный план; /shopping — последний.
  readonly planId = input<string>('');

  readonly items = signal<ShoppingListItem[]>([]);
  readonly loading = signal(true);
  readonly empty = signal(false);
  readonly title = signal('');
  readonly currentPlanId = signal('');

  private readonly checked = signal<Set<string>>(new Set());
  private activePlanId = '';

  // Живая группировка по категориям — пересобирается по мере прихода пунктов.
  readonly groups = computed<ShoppingGroup[]>(() => {
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
      items: [...byCat.get(category)!].sort((a, b) => a.name.localeCompare(b.name, 'ru')),
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

  private load(planId: string): void {
    this.loading.set(true);
    this.empty.set(false);
    this.items.set([]);

    if (planId) {
      this.fetch(planId);
      return;
    }
    // Без явного плана — берём последний (принятый в приоритете).
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

  private fetch(planId: string): void {
    this.activePlanId = planId;
    this.currentPlanId.set(planId);
    this.checked.set(this.loadChecked(planId));
    this.items.set([]);

    void this.api.shoppingStream(planId, {
      onItem: (item) => this.items.update((list) => [...list, item]),
      onDone: () => {
        this.loading.set(false);
        this.empty.set(this.items().length === 0);
      },
      onError: () => {
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
