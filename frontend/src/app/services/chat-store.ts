import { Injectable, inject, signal } from '@angular/core';
import { ChatMessage, WeekPlan } from '../models/plan.model';
import { EXTRA_DISHES } from '../data/mock-plan';
import { EasyWeekApi } from './api';

const INTRO: ChatMessage = {
  id: 'intro',
  role: 'assistant',
  text: 'Привет! Составлю меню на неделю под заморозку. Сколько ужинов и есть ли ограничения?',
};

// Общий стор чата: состояние переживает переходы (план → рецепт → назад),
// поэтому на вкладке «Чат» всегда открыт последний активный чат.
@Injectable({ providedIn: 'root' })
export class ChatStore {
  private readonly api = inject(EasyWeekApi);

  readonly messages = signal<ChatMessage[]>([INTRO]);
  readonly draft = signal('');
  readonly loading = signal(false);
  // id сообщения-плана, который сейчас стримится (лоадер живёт внутри его карточки)
  readonly streamingMsgId = signal<string | null>(null);
  readonly dishCount = signal(5);

  private conversationId: string | null = null;
  private seq = 100;

  setCount(n: number): void {
    this.dishCount.set(n);
  }

  // Начать новый чат (сбрасываем переписку, но сохраняем выбор количества).
  newChat(): void {
    this.messages.set([INTRO]);
    this.conversationId = null;
    this.draft.set('');
    this.loading.set(false);
  }

  // Загрузить существующий диалог плана (для «Продолжить обсуждение»).
  loadConversation(conversationId: string): void {
    this.conversationId = conversationId;
    this.draft.set('');
    this.loading.set(true);
    this.api.conversationMessages(conversationId).subscribe({
      next: (msgs) => {
        this.messages.set(msgs.length ? msgs : [INTRO]);
        this.loading.set(false);
      },
      error: () => {
        this.messages.set([INTRO]);
        this.loading.set(false);
      },
    });
  }

  send(): void {
    const text = this.draft().trim();
    if (!text || this.loading()) return;

    this.messages.update((list) => [...list, { id: `u-${this.seq++}`, role: 'user', text }]);
    this.draft.set('');
    this.loading.set(true);

    // Если в диалоге уже есть план — это правка (tool calling), а не новый план.
    if (this.conversationId && this.messages().some((m) => m.plan)) {
      this.editCurrentPlan(text);
      return;
    }

    // Потоковый приём: сначала meta (карточка плана), потом блюда по одному.
    const msgId = `a-${this.seq++}`;
    let planStarted = false;

    void this.api.chatStream(text, this.conversationId, this.dishCount(), {
      onMeta: (m) => {
        this.conversationId = m.conversationId;
        planStarted = true;
        this.streamingMsgId.set(msgId);
        this.messages.update((list) => [
          ...list,
          {
            id: msgId,
            role: 'assistant',
            text: m.reply,
            plan: {
              id: m.planId,
              conversationId: m.conversationId,
              title: m.title,
              weekLabel: m.weekLabel,
              status: 'draft',
              provider: m.provider,
              dishes: [],
            },
          },
        ]);
      },
      onDish: (dish) => {
        this.updatePlan(msgId, (plan) => ({ ...plan, dishes: [...plan.dishes, dish] }));
      },
      onDone: () => {
        this.streamingMsgId.set(null);
        this.loading.set(false);
      },
      onError: (message) => {
        this.streamingMsgId.set(null);
        if (!planStarted) {
          this.messages.update((list) => [
            ...list,
            {
              id: `e-${this.seq++}`,
              role: 'assistant',
              text:
                message === 'Не удалось составить план'
                  ? 'Не получилось составить план. Попробуйте ещё раз.'
                  : `${message}. Попробуйте ещё раз.`,
            },
          ]);
        }
        this.loading.set(false);
      },
    });
  }

  // Правка текущего плана: обновляем карточку на месте + добавляем реплику бота.
  private editCurrentPlan(text: string): void {
    const convId = this.conversationId;
    if (!convId) {
      this.loading.set(false);
      return;
    }
    this.api.editPlan(convId, text).subscribe({
      next: (res) => {
        const plan = res.plan;
        if (plan) {
          // Правка вернула НОВУЮ версию плана (новый id) — заменяем ею текущую карточку
          // (последнее сообщение с планом). Старый план остаётся доступен по своей ссылке.
          this.messages.update((list) => {
            let lastIdx = -1;
            list.forEach((m, i) => {
              if (m.plan) lastIdx = i;
            });
            return lastIdx === -1
              ? list
              : list.map((m, i) => (i === lastIdx ? { ...m, plan } : m));
          });
        }
        this.messages.update((list) => [
          ...list,
          { id: `a-${this.seq++}`, role: 'assistant', text: res.reply },
        ]);
        this.loading.set(false);
      },
      error: () => {
        this.messages.update((list) => [
          ...list,
          {
            id: `e-${this.seq++}`,
            role: 'assistant',
            text: 'Не получилось изменить план. Попробуйте ещё раз.',
          },
        ]);
        this.loading.set(false);
      },
    });
  }

  setPlanStatus(
    messageId: string,
    planId: string,
    status: 'accepted' | 'rejected',
    onSuccess?: () => void,
  ): void {
    this.api.setStatus(planId, status).subscribe({
      next: (updated) => {
        this.updatePlan(messageId, () => updated);
        onSuccess?.();
      },
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
