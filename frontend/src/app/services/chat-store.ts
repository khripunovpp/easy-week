import { Injectable, computed, inject, linkedSignal, signal } from '@angular/core';
import { ChatMessage, DiscussTarget, WeekPlan } from '../models/plan.model';
import { EasyWeekApi } from './api';
import { ModelSettings } from './model-settings';
import { PlanWizard, composeRequest } from '../shared/plan-wizard';
import { ALL_MODELS, RecipeModel } from './preferences';

// Действие, инициированное кнопкой карточки, — «висит» бейджем в композере до отправки.
// discuss — режим «Обсуждение: <что>» (кнопка «💬 Обсудить в чате» на странице рецепта /
// готовки / покупок): липкий — не снимается после отправки, только крестиком.
export type PendingAction =
  | { kind: 'replace'; id: string; name: string }
  | { kind: 'add' }
  | { kind: 'discuss'; target: DiscussTarget; planId: string; dishId?: string; name: string };

// Параметры входа в режим обсуждения со страницы.
export interface DiscussStart {
  conversationId: string;
  target: DiscussTarget;
  planId: string;
  dishId?: string;
  name: string;
  model?: string; // модель открытого варианта — её же ставим в чат (обсуждаем то, что видим)
}

const INTRO: ChatMessage = {
  id: 'intro',
  role: 'assistant',
  text: 'Привет! Составлю меню на неделю под заморозку. Натыкайте варианты ниже или просто напишите, чего хочется.',
};

// Провайдер плана (человекочитаемый, из бэка) → ключ модели чата.
const PROVIDER_TO_MODEL: Record<string, RecipeModel> = {
  DeepSeek: 'deepseek',
  Gemini: 'gemini',
  Claude: 'anthropic',
  Cloudflare: 'cloudflare',
};

// Общий стор чата: состояние переживает переходы (план → рецепт → назад),
// поэтому на вкладке «Чат» всегда открыт последний активный чат.
@Injectable({ providedIn: 'root' })
export class ChatStore {
  private readonly api = inject(EasyWeekApi);
  private readonly modelSettings = inject(ModelSettings);
  // Быстрый выбор в новом чате: карточки → строка запроса (текст пользователя — уточнение, важнее).
  readonly wizard = inject(PlanWizard);

  readonly messages = signal<ChatMessage[]>([INTRO]);
  readonly draft = signal('');
  readonly loading = signal(false);
  // id сообщения-плана, который сейчас стримится (лоадер живёт внутри его карточки)
  readonly streamingMsgId = signal<string | null>(null);
  // Тик для прокрутки ленты вниз (к новой карточке плана после правки).
  readonly scrollBump = signal(0);
  readonly dishCount = signal(5);
  // Модель этого чата (override). Стартует с настройки «Чат и план» (сервер, общая для семьи)
  // и следует за ней, пока в чате не выбрали свою; переключение здесь настройки НЕ меняет.
  readonly recipeModel = linkedSignal<RecipeModel>(() => this.modelSettings.models().chat);
  // Действие с карточки, ждущее отправки (бейдж в композере): замена блюда или добавление.
  readonly pending = signal<PendingAction | null>(null);

  conversationId: string | null = null; // публичный — нужен для оценки сообщений
  private seq = 100;

  setCount(n: number): void {
    this.dishCount.set(n);
  }

  // Переключение модели в чате — только в рамках этого чата, настройки не трогаем.
  setModel(m: RecipeModel): void {
    this.recipeModel.set(m);
  }

  // Начать новый чат (сбрасываем переписку, но сохраняем выбор количества).
  // Модель чата пере-сеиваем из актуальной настройки «Чат и план».
  newChat(): void {
    this.wizard.reset();
    this.messages.set([INTRO]);
    this.conversationId = null;
    this.draft.set('');
    this.loading.set(false);
    this.pending.set(null); // бейдж прошлого чата (замена/обсуждение) в новый не переносим
    this.recipeModel.set(this.modelSettings.models().chat);
  }

  // Войти в режим «Обсуждение»: открыть беседу плана и поставить бейдж в композер.
  // Если эта беседа уже открыта — не перезагружаем ленту (сохраняем прокрутку/стрим).
  startDiscuss(opts: DiscussStart): void {
    const model = ALL_MODELS.includes(opts.model as RecipeModel)
      ? (opts.model as RecipeModel)
      : undefined;
    if (this.conversationId !== opts.conversationId || this.messages().length <= 1) {
      this.loadConversation(opts.conversationId, model);
    } else if (model) {
      this.recipeModel.set(model);
    }
    this.draft.set('');
    this.pending.set({
      kind: 'discuss',
      target: opts.target,
      planId: opts.planId,
      dishId: opts.dishId,
      name: opts.name,
    });
  }

  // Загрузить существующий диалог плана (для «Продолжить обсуждение»).
  // model — явная модель чата (обсуждение варианта конкретной модели); иначе — модель плана.
  loadConversation(conversationId: string, model?: RecipeModel): void {
    this.conversationId = conversationId;
    this.draft.set('');
    this.loading.set(true);
    this.recipeModel.set(this.modelSettings.models().chat);
    this.api.conversationMessages(conversationId).subscribe({
      next: (msgs) => {
        // У загруженных сообщений id = серверный → сразу доступны для оценки.
        this.messages.set(msgs.length ? msgs.map((m) => this.fromServer(m)) : [INTRO]);
        // Модель чата — по последнему плану диалога: правки/рецепты идут той же моделью,
        // что собрала план, а не дефолтом из настроек (выставленным выше как фолбэк).
        // Явно переданная модель (обсуждение открытого варианта) важнее.
        if (model) this.recipeModel.set(model);
        else this.syncModelToLastPlan(msgs);
        this.loading.set(false);
        // Лента приезжает уже после перехода в чат (из плана/покупок/рецепта) — «тик»,
        // чтобы чат прижался к низу, а не остался наверху.
        this.scrollBump.update((n) => n + 1);
      },
      error: () => {
        this.messages.set([INTRO]);
        this.loading.set(false);
      },
    });
  }

  // Сообщение с сервера → ленточное: серверный id (для оценки) + привязка реплики обсуждения.
  private fromServer(m: ChatMessage): ChatMessage {
    const out: ChatMessage = { ...m, serverId: m.id };
    if (m.discussTarget && m.role === 'assistant' && m.discussPlanId) {
      out.discuss = {
        target: m.discussTarget,
        planId: m.discussPlanId,
        dishId: m.dishId ?? undefined,
      };
    }
    return out;
  }

  // Подстроить модель чата под провайдера последнего плана диалога (если распознан).
  private syncModelToLastPlan(msgs: ChatMessage[]): void {
    let provider = '';
    for (const m of msgs) if (m.plan?.provider) provider = m.plan.provider;
    const model = PROVIDER_TO_MODEL[provider];
    if (model) this.recipeModel.set(model);
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

  /** Новый чат: ещё ничего не отправляли (только приветствие) — показываем быстрый выбор. */
  readonly fresh = computed(() => {
    const list = this.messages();
    return list.length === 1 && list[0].id === INTRO.id;
  });

  send(): void {
    const pending = this.pending();
    let text = this.draft().trim();
    // Первое сообщение нового чата: выбор на карточках + уточнение текстом (оно важнее выбора).
    if (!this.conversationId && !pending && this.fresh() && this.wizard.choice()) {
      text = composeRequest(this.wizard.choice(), text);
      const n = this.wizard.count();
      if (n) this.dishCount.set(n); // явное число в тексте всё равно важнее (промпт плана)
    }
    if ((!text && !pending) || this.loading()) return;
    this.wizard.reset();

    // Режим «Обсуждение» — ДО ветки правки плана: версий плана не создаём, бейдж остаётся.
    if (pending?.kind === 'discuss') {
      if (text) this.sendDiscuss(pending, text);
      return;
    }

    const userText = this.pendingLabel(pending, text);
    this.messages.update((list) => [
      ...list,
      { id: `u-${this.seq++}`, role: 'user', text: userText },
    ]);
    this.draft.set('');
    this.loading.set(true);

    // Правка текущего плана: по кнопке (минуя тул-коллинг) или tool calling по тексту.
    // Последний план беседы отклонён — правит нечего: реплика собирает НОВЫЙ план (в этой же
    // беседе, с её памятью), иначе правка отвечала «меню уже составлено».
    const lastPlan = [...this.messages()].reverse().find((m) => m.plan)?.plan;
    const editable = !!lastPlan && lastPlan.status !== 'rejected';
    if (this.conversationId && (pending || editable)) {
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

    void this.api.chatStream(text, this.conversationId, this.dishCount(), this.recipeModel(), {
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
        // Остатки модель отдаёт в конце потока — дописываем в карточку плана.
        if (info.leftovers?.length) {
          this.updatePlan(msgId, (plan) => ({ ...plan, leftovers: info.leftovers }));
        }
        // Проставляем серверный id + модель стримленному сообщению — чтобы можно было оценить.
        if (info.messageId) {
          this.messages.update((list) =>
            list.map((m) =>
              m.id === msgId ? { ...m, serverId: info.messageId, model: info.model } : m,
            ),
          );
        }
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
                  ? 'Не получилось составить план этой моделью. Переключите модель выше или попробуйте ещё раз.'
                  : `${message}. Переключите модель выше или попробуйте ещё раз.`,
            },
          ]);
        }
        this.loading.set(false);
      },
    });
  }

  // Реплика в режиме обсуждения: ответ бота + ссылка на цель; «замени блюдо» → кнопка замены.
  private sendDiscuss(pending: Extract<PendingAction, { kind: 'discuss' }>, text: string): void {
    this.messages.update((list) => [...list, { id: `u-${this.seq++}`, role: 'user', text }]);
    this.draft.set('');
    this.loading.set(true);
    this.api
      .discuss({
        conversationId: this.conversationId,
        planId: pending.planId,
        target: pending.target,
        dishId: pending.dishId,
        message: text,
        recipeModel: this.recipeModel(),
      })
      .subscribe({
        next: (res) => {
          this.conversationId = res.conversationId;
          this.messages.update((list) => [
            ...list,
            {
              id: `a-${this.seq++}`,
              role: 'assistant',
              text: res.reply,
              serverId: res.messageId,
              model: res.model,
              discuss: {
                target: res.target,
                planId: res.planId,
                dishId: res.dishId ?? undefined,
              },
              suggestReplace:
                res.suggestReplace && res.dishId
                  ? { dishId: res.dishId, name: pending.name, query: res.replaceQuery }
                  : undefined,
            },
          ]);
          this.loading.set(false);
          this.scrollBump.update((n) => n + 1);
        },
        error: (err) => {
          const detail = err?.error?.detail as string | undefined;
          this.messages.update((list) => [
            ...list,
            {
              id: `e-${this.seq++}`,
              role: 'assistant',
              text:
                detail ||
                'Обсуждение недоступно этой моделью. Переключите модель выше или попробуйте ещё раз.',
            },
          ]);
          this.loading.set(false);
        },
      });
  }

  // «Заменить блюдо» под ответом обсуждения: бейдж → режим замены, пожелание — в черновик.
  acceptReplaceSuggestion(s: { dishId: string; name: string; query: string }): void {
    this.requestReplace(s.dishId, s.name);
    this.draft.set(s.query);
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
    this.api.editPlan(convId, text, this.recipeModel(), opts).subscribe({
      next: (res) => {
        const msgId = `a-${this.seq++}`;
        this.messages.update((list) => {
          // Прошлую (развёрнутую) карточку сворачиваем как отклонённую — она остаётся
          // в истории выше; новую версию плана вешаем на реплику «Готово…» внизу ленты.
          let next = list;
          if (res.plan) {
            let liveIdx = -1;
            list.forEach((m, i) => {
              if (m.plan && m.plan.status !== 'rejected') liveIdx = i;
            });
            if (liveIdx >= 0) {
              next = list.map((m, i) =>
                i === liveIdx ? { ...m, plan: { ...m.plan!, status: 'rejected' as const } } : m,
              );
            }
          }
          return [
            ...next,
            {
              id: msgId,
              role: 'assistant',
              text: res.reply,
              plan: res.plan ?? undefined,
              serverId: res.messageId,
              model: res.model,
            },
          ];
        });
        this.loading.set(false);
        this.scrollBump.update((n) => n + 1);
      },
      error: (err) => {
        // 429 (дневной лимит Claude) и пр. — показываем текст с бэка, если есть
        const detail = err?.error?.detail as string | undefined;
        this.messages.update((list) => [
          ...list,
          {
            id: `e-${this.seq++}`,
            role: 'assistant',
            text:
              detail ||
              'Не получилось изменить план этой моделью. Переключите модель выше или попробуйте ещё раз.',
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
    this.api.editPlan(convId, '', this.recipeModel(), { removeDishId: dishId }).subscribe({
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
