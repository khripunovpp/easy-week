import { Component, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ChatMessage, WeekPlan } from '../../models/plan.model';
import { EXTRA_DISHES, MOCK_MESSAGES, MOCK_PLAN } from '../../data/mock-plan';

@Component({
  selector: 'ew-chat',
  imports: [FormsModule, RouterLink],
  templateUrl: './chat.html',
  styleUrl: './chat.scss',
})
export class Chat {
  readonly messages = signal<ChatMessage[]>(MOCK_MESSAGES);
  readonly draft = signal('');

  readonly suggestions = ['5 ужинов', 'Без свинины', 'На 2 порции', 'Побыстрее'];

  private seq = 100;

  pastel(i: number): string {
    return `pastel-${i % 5}`;
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  useSuggestion(text: string): void {
    this.draft.set(text);
  }

  // Убрать одно блюдо из плана, не отклоняя весь план.
  removeDish(messageId: string, dishId: string): void {
    this.updatePlan(messageId, (plan) => ({
      ...plan,
      dishes: plan.dishes.filter((d) => d.id !== dishId),
    }));
  }

  // Добавить блюдо в план (пока — из запасного пула; позже — по запросу к AI).
  addDish(messageId: string): void {
    this.updatePlan(messageId, (plan) => {
      const present = new Set(plan.dishes.map((d) => d.id));
      const next = EXTRA_DISHES.find((d) => !present.has(d.id));
      return next ? { ...plan, dishes: [...plan.dishes, next] } : plan;
    });
  }

  private updatePlan(messageId: string, fn: (plan: WeekPlan) => WeekPlan): void {
    this.messages.update((list) =>
      list.map((m) =>
        m.id === messageId && m.plan ? { ...m, plan: fn(m.plan) } : m,
      ),
    );
  }

  send(): void {
    const text = this.draft().trim();
    if (!text) return;

    this.messages.update((list) => [
      ...list,
      { id: `u-${this.seq++}`, role: 'user', text },
    ]);
    this.draft.set('');

    // Заглушка ответа, пока нет бэкенда: показываем тот же план.
    setTimeout(() => {
      this.messages.update((list) => [
        ...list,
        {
          id: `a-${this.seq++}`,
          role: 'assistant',
          text: 'Обновил меню под запрос — всё под заморозку 👇',
          plan: { ...MOCK_PLAN, id: `plan-${this.seq}` },
        },
      ]);
    }, 350);
  }
}
