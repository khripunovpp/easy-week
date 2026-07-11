import { Component, computed, signal } from '@angular/core';
import { MOCK_PLAN } from '../../data/mock-plan';
import { ShoppingItem } from '../../models/plan.model';
import { buildShoppingList, shoppingKey } from '../../services/shopping';

@Component({
  selector: 'ew-shopping',
  imports: [],
  templateUrl: './shopping.html',
  styleUrl: './shopping.scss',
})
export class Shopping {
  readonly plan = MOCK_PLAN;
  readonly groups = buildShoppingList(this.plan);

  private readonly checked = signal<Set<string>>(new Set());

  readonly total = this.groups.reduce((n, g) => n + g.items.length, 0);
  readonly doneCount = computed(() => this.checked().size);

  isChecked(item: ShoppingItem): boolean {
    return this.checked().has(shoppingKey(item));
  }

  toggle(item: ShoppingItem): void {
    const key = shoppingKey(item);
    this.checked.update((set) => {
      const next = new Set(set);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  fmtQty(item: ShoppingItem): string {
    return `${item.qty} ${item.unit}`;
  }
}
