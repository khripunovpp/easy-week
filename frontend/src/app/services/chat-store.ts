import { Injectable, inject, signal } from '@angular/core';
import { ChatMessage, WeekPlan } from '../models/plan.model';
import { EasyWeekApi } from './api';

// Действие, инициированное кнопкой карточки, — «висит» бейджем в композере до отправки.
export type PendingAction =
  | { kind: 'replace'; id: string; name: string }
  | { kind: 'add' };

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
  // Действие с карточки, ждущее отправки (бейдж в композере): замена блюда или добавление.
  readonly pending = signal<PendingAction | null>(null);

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

  // Кнопки карточки: пометить блюдо к замене / добавить блюдо / снять пометку (бейдж в композере).
  requestReplace(id: string, name: string): void {
    this.pending.set({ kind: 'replace', id, name });
  }
  requestAdd(): void {
    this.pending.set({ kind: 'add' });
  }
  clearPending(): void {
    this.pending.set(null);
  }

  send(): void {
    const pending = this.pending();
    const text = this.draft().trim();
    if ((!text && !pending) || this.loading()) return;

    const userText = this.pendingLabel(pending, text);
    this.messages.update((list) => [
      ...list,
      { id: `u-${this.seq++}`, role: 'user', text: userText },
    ]);
    this.draft.set('');
    this.loading.set(true);

    // Правка текущего плана: по кнопке (минуя тул-коллинг) или tool calling по тексту.
    if (this.conversationId && (pending || this.messages().some((m) => m.plan))) {
      this.pending.set(null);
      const opts =
        pending?.kind === 'replace'
          ? { replaceDishId: pending.id }
          : pending?.kind === 'add'
            ? { addDish: true }
            : {};
      this.editCurrentPlan(text, opts);
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
      onDone: (info) => {
        this.streamingMsgId.set(null);
        this.loading.set(false);
        // Число блюд решает модель (пользователь мог указать другое в тексте) —
        // приводим счётчик в шапке к фактическому результату.
        if (info.dishesCount >= 1 && info.dishesCount <= 12) {
          this.dishCount.set(info.dishesCount);
        }
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

  // Лейбл действия для ленты («Замена «X»», «Добавить блюдо») + дописанный пользователем текст.
  private pendingLabel(pending: PendingAction | null, text: string): string {
    if (pending?.kind === 'replace') return `Замена «${pending.name}»${text ? `: ${text}` : ''}`;
    if (pending?.kind === 'add') return `Добавить блюдо${text ? `: ${text}` : ''}`;
    return text;
  }

  // Правка текущего плана: обновляем карточку на месте + добавляем реплику бота.
  private editCurrentPlan(
    text: string,
    opts: { replaceDishId?: string; addDish?: boolean } = {},
  ): void {
    const convId = this.conversationId;
    if (!convId) {
      this.loading.set(false);
      return;
    }
    this.api.editPlan(convId, text, opts).subscribe({
      next: (res) => {
        if (res.plan) this.replaceCurrentPlan(res.plan);
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

  // Заменяет план в последнем сообщении с планом на новую версию (id меняется).
  private replaceCurrentPlan(plan: WeekPlan): void {
    this.messages.update((list) => {
      let lastIdx = -1;
      list.forEach((m, i) => {
        if (m.plan) lastIdx = i;
      });
      return lastIdx === -1 ? list : list.map((m, i) => (i === lastIdx ? { ...m, plan } : m));
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

  // Крестик: детерминированное удаление блюда (без модели). Карточку обновляем на месте,
  // сообщений в ленту не добавляем — новая версия приходит с сервера.
  removeDish(dishId: string): void {
    const convId = this.conversationId;
    if (!convId || this.loading()) return;
    this.loading.set(true);
    this.api.editPlan(convId, '', { removeDishId: dishId }).subscribe({
      next: (res) => {
        if (res.plan) this.replaceCurrentPlan(res.plan);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  private updatePlan(messageId: string, fn: (plan: WeekPlan) => WeekPlan): void {
    this.messages.update((list) =>
      list.map((m) => (m.id === messageId && m.plan ? { ...m, plan: fn(m.plan) } : m)),
    );
  }
}
