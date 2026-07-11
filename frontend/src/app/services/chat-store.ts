import { Injectable, inject, signal } from '@angular/core';
import { ChatMessage, WeekPlan } from '../models/plan.model';
import { EXTRA_DISHES } from '../data/mock-plan';
import { EasyWeekApi } from './api';

// Общий стор чата: состояние переживает переходы (план → рецепт → назад).
@Injectable({ providedIn: 'root' })
export class ChatStore {
  private readonly api = inject(EasyWeekApi);

  readonly messages = signal<ChatMessage[]>([
    {
      id: 'intro',
      role: 'assistant',
      text: 'Привет! Составлю меню на неделю под заморозку. Сколько ужинов и есть ли ограничения?',
    },
  ]);
  readonly draft = signal('');
  readonly loading = signal(false);
  readonly dishCount = signal(5);

  private conversationId: string | null = null;
  private seq = 100;

  setCount(n: number): void {
    this.dishCount.set(n);
  }

  send(): void {
    const text = this.draft().trim();
    if (!text || this.loading()) return;

    this.messages.update((list) => [...list, { id: `u-${this.seq++}`, role: 'user', text }]);
    this.draft.set('');
    this.loading.set(true);

    this.api.chat(text, this.conversationId, this.dishCount()).subscribe({
      next: (res) => {
        this.conversationId = res.conversationId;
        this.messages.update((list) => [
          ...list,
          { id: `a-${this.seq++}`, role: 'assistant', text: res.reply, plan: res.plan ?? undefined },
        ]);
        this.loading.set(false);
      },
      error: () => {
        this.messages.update((list) => [
          ...list,
          {
            id: `e-${this.seq++}`,
            role: 'assistant',
            text: 'Не получилось составить план. Проверьте, что бэкенд запущен, и попробуйте ещё раз.',
          },
        ]);
        this.loading.set(false);
      },
    });
  }

  setPlanStatus(messageId: string, planId: string, status: 'accepted' | 'rejected'): void {
    this.api.setStatus(planId, status).subscribe({
      next: (updated) => this.updatePlan(messageId, () => updated),
    });
  }

  removeDish(messageId: string, dishId: string): void {
    this.updatePlan(messageId, (plan) => ({
      ...plan,
      dishes: plan.dishes.filter((d) => d.id !== dishId),
    }));
  }

  addDish(messageId: string): void {
    this.updatePlan(messageId, (plan) => {
      const present = new Set(plan.dishes.map((d) => d.id));
      const next = EXTRA_DISHES.find((d) => !present.has(d.id));
      return next ? { ...plan, dishes: [...plan.dishes, next] } : plan;
    });
  }

  private updatePlan(messageId: string, fn: (plan: WeekPlan) => WeekPlan): void {
    this.messages.update((list) =>
      list.map((m) => (m.id === messageId && m.plan ? { ...m, plan: fn(m.plan) } : m)),
    );
  }
}
