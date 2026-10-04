import { WeekPlan } from '../models/plan.model';

// Остаток пользователя в строке «🧺 Остатки» карточки плана: пристроен ли он в какое-то блюдо
// (есть в dish.uses). Не пристроенный показываем приглушённым — модель честно не нашла места.
export interface LeftoverChip {
  name: string;
  placed: boolean;
}

export function leftoverChips(plan: WeekPlan): LeftoverChip[] {
  const used = new Set(plan.dishes.flatMap((d) => d.uses ?? []).map((u) => u.toLowerCase()));
  return (plan.leftovers ?? []).map((name) => ({ name, placed: used.has(name.toLowerCase()) }));
}
