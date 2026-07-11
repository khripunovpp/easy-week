import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ChatMessage, WeekPlan } from '../../models/plan.model';
import { EXTRA_DISHES } from '../../data/mock-plan';
import { EasyWeekApi } from '../../services/api';

@Component({
  selector: 'ew-chat',
  imports: [FormsModule, RouterLink],
  templateUrl: './chat.html',
  styleUrl: './chat.scss',
})
export class Chat {
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

  readonly suggestions = ['5 ужинов', 'Без свинины', 'На 2 порции', 'Побыстрее'];

  private conversationId: string | null = null;
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

  send(): void {
    const text = this.draft().trim();
    if (!text || this.loading()) return;

    this.messages.update((list) => [...list, { id: `u-${this.seq++}`, role: 'user', text }]);
    this.draft.set('');
    this.loading.set(true);

    this.api.chat(text, this.conversationId).subscribe({
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

  // Принять / отклонить план — сохраняем статус на бэкенде.
  setPlanStatus(messageId: string, planId: string, status: 'accepted' | 'rejected'): void {
    this.api.setStatus(planId, status).subscribe({
      next: (updated) => this.updatePlan(messageId, () => updated),
    });
  }

  // Убрать блюдо из показанного плана (визуально, не отклоняя план).
  removeDish(messageId: string, dishId: string): void {
    this.updatePlan(messageId, (plan) => ({
      ...plan,
      dishes: plan.dishes.filter((d) => d.id !== dishId),
    }));
  }

  // Добавить блюдо (пока из запасного пула; далее — запрос к AI).
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
