import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { EasyWeekApi, ShoppingGroup } from '../../services/api';

@Component({
  selector: 'ew-shopping',
  imports: [RouterLink],
  templateUrl: './shopping.html',
  styleUrl: './shopping.scss',
})
export class Shopping {
  private readonly api = inject(EasyWeekApi);

  // /shopping/:planId — конкретный план; /shopping — последний.
  readonly planId = input<string>('');

  readonly groups = signal<ShoppingGroup[]>([]);
  readonly loading = signal(true);
  readonly empty = signal(false);
  readonly title = signal('');
  readonly currentPlanId = signal('');

  private readonly checked = signal<Set<string>>(new Set());
  private activePlanId = '';

  readonly total = computed(() =>
    this.groups().reduce((n, g) => n + g.items.length, 0),
  );
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
    this.api.shoppingList(planId).subscribe({
      next: (groups) => {
        this.groups.set(groups);
        this.empty.set(groups.length === 0);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.empty.set(true);
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
