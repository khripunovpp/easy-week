import { IngredientCategory, ShoppingItem, WeekPlan } from '../models/plan.model';

// Порядок категорий в списке покупок.
const CATEGORY_ORDER: IngredientCategory[] = [
  'Мясо и птица',
  'Рыба',
  'Овощи',
  'Молочное',
  'Бакалея',
  'Специи',
  'Прочее',
];

export interface ShoppingGroup {
  category: IngredientCategory;
  items: ShoppingItem[];
}

// Агрегирует ингредиенты всех блюд плана: суммирует по (name + unit),
// группирует по категориям в заданном порядке.
export function buildShoppingList(plan: WeekPlan): ShoppingGroup[] {
  const merged = new Map<string, ShoppingItem>();

  for (const dish of plan.dishes) {
    for (const ing of dish.ingredients) {
      const key = `${ing.name.toLowerCase()}__${ing.unit}`;
      const existing = merged.get(key);
      if (existing) {
        existing.qty += ing.qty;
      } else {
        merged.set(key, {
          name: ing.name,
          qty: ing.qty,
          unit: ing.unit,
          category: ing.category,
          checked: false,
        });
      }
    }
  }

  const byCategory = new Map<IngredientCategory, ShoppingItem[]>();
  for (const item of merged.values()) {
    const list = byCategory.get(item.category) ?? [];
    list.push(item);
    byCategory.set(item.category, list);
  }

  return CATEGORY_ORDER.filter((c) => byCategory.has(c)).map((category) => ({
    category,
    items: byCategory.get(category)!.sort((a, b) => a.name.localeCompare(b.name, 'ru')),
  }));
}

export function shoppingKey(item: ShoppingItem): string {
  return `${item.name.toLowerCase()}__${item.unit}`;
}
