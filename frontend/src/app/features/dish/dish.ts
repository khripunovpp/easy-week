import { Location } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dish, LIBRARY_PLAN_ID } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { ModelSettings } from '../../services/model-settings';
import { CookingLoader } from '../../shared/cooking-loader';
import { Vote } from '../../shared/vote';
import { formatGeneratedAt } from '../../shared/format';
import { aiFailText } from '../../shared/ai-error';
import { ModelName } from '../../shared/model-name';
import { TtsBtn } from '../../shared/tts-btn';
import { Modal } from '../../shared/modal';

@Component({
  selector: 'ew-dish',
  imports: [RouterLink, CookingLoader, Vote, ModelName, TtsBtn, Modal],
  templateUrl: './dish.html',
  styleUrl: './dish.scss',
})
export class DishPage {
  /** Подпись даты генерации: «сегодня, 14:05» / «26 сен, 14:05»; пусто — не показываем. */
  genAt(iso: string | null | undefined): string {
    return formatGeneratedAt(iso);
  }

  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly location = inject(Location);
  private readonly router = inject(Router);

  readonly planId = input<string>('');
  readonly dishId = input<string>('');
  // ?model=<ключ> — открыть вариант конкретной модели (напр. из готовки — модель плана).
  readonly model = input<string>('');

  // Назад — туда, откуда пришли (план или чат). Если истории нет — на план.
  back(): void {
    if (history.length > 1) {
      this.location.back();
    } else {
      // Свой рецепт — к «Рецептам» в Книге (у библиотеки нет страницы плана).
      this.router.navigate(this.isOwn() ? ['/plans'] : ['/plan', this.planId()]);
    }
  }

  /** Свой рецепт пользователя («Мои рецепты», модалка «Свой рецепт»). */
  readonly isOwn = computed(() => this.planId() === LIBRARY_PLAN_ID);

  readonly dish = signal<Dish | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);
  readonly errorMsg = signal('');
  readonly modelMenuOpen = signal(false);
  readonly generatingModel = signal<string | null>(null); // модель, чей вариант сейчас генерится
  // «↻ Перегенерировать»: идёт генерация нового варианта (текущий рецепт остаётся на экране)
  // и текст ошибки последней попытки (показываем в футере с «Повторить», блюдо не стираем).
  readonly regenerating = signal(false);
  readonly regenError = signal('');
  // Выбор модели в выпадашке не удался (модель перегружена/не ответила), а на экране уже есть
  // рецепт — он остаётся, ошибка с «Повторить» в футере (иначе старая версия молча её перекрывала).
  readonly switchError = signal<{ model: string; text: string } | null>(null);
  // Рецепт не собрался и показать нечего (лимит Claude, модель упала): какая модель не смогла и
  // другие модели задачи «Рецепты» — кнопками прямо под ошибкой, тап сразу генерирует.
  readonly failedModel = signal('');
  // Лимит (429) той же моделью не повторяем — только перегрузка/сбой.
  readonly failedRetryable = signal(false);
  readonly retryModels = computed<RecipeModel[]>(() =>
    this.modelSettings.modelsFor('recipe').filter((m) => m !== this.failedModel()),
  );
  private readonly opening = signal(false); // «💬 Обсудить»: ждём conversationId плана
  readonly busy = computed(() => this.regenerating() || this.opening());

  private readonly modelSettings = inject(ModelSettings);
  // Модели, для которых варианта рецепта ещё нет — в выпадашке показываем со стрелкой ↓.
  // Только годные для рецептов (карта задач): дешёвые Cloudflare/OpenRouter не предлагаем;
  // уже сгенерированные ими старые варианты при этом остаются в списке (variantModels).
  readonly remainingModels = computed<RecipeModel[]>(() => {
    const have = new Set(this.dish()?.variantModels ?? []);
    return this.modelSettings.modelsFor('recipe').filter((m) => !have.has(m));
  });

  constructor() {
    // Догружаем блюдо (с ленивой генерацией шагов на бэкенде) при смене маршрута.
    effect(() => {
      const pid = this.planId();
      const did = this.dishId();
      if (!pid || !did) return;
      // Если пришли с ?model= (напр. из готовки — модель плана) — открываем именно её вариант
      // (сгенерится, если ещё нет); иначе — активный вариант, а если рецепта ещё нет —
      // модель пустая: бэк возьмёт настройку «Рецепты» (общая, на сервере).
      const m = this.model();
      if (m) this.load(pid, did, m, 'select');
      else this.load(pid, did, '', 'open');
    });
  }

  private load(pid: string, did: string, model: string, action: 'open' | 'select'): void {
    this.loading.set(true);
    this.failed.set(false);
    this.api.dishDetails(pid, did, model, action).subscribe({
      next: (d) => {
        this.dish.set(d);
        this.switchError.set(null);
        this.loading.set(false);
        this.generatingModel.set(null);
        this.modelMenuOpen.set(false);
      },
      error: (err) => {
        // Пустая модель — бэк взял дефолт задачи «Рецепты»: называем её в тексте и кнопке повтора.
        const used = (model || this.modelSettings.models().recipe).split(':')[0];
        const text = aiFailText(err, used, 'рецепт не собран');
        if (action === 'select' && this.dish()) {
          this.switchError.set({ model, text });
          this.loading.set(false);
          this.generatingModel.set(null);
          this.modelMenuOpen.set(false);
          return;
        }
        // 429 (дневной лимит) — текст с бэка; 502 — понятный текст вместо сырого ответа провайдера
        this.errorMsg.set(text);
        this.failedModel.set(used);
        this.failedRetryable.set(err?.status !== 429);
        this.failed.set(true);
        this.loading.set(false);
        this.generatingModel.set(null);
        this.modelMenuOpen.set(false);
      },
    });
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  /** Конкретная модель, которой соберут новый вариант (модель задачи из настроек, если тот же провайдер). */
  newRef(key: string): string {
    return this.modelSettings.refFor('recipe', key);
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  // Выбор модели в выпадашке: делаем вариант активным (генерим на бэке, если его ещё нет).
  // Уже активная — просто закрыть. Для несгенерённой держим меню открытым со спиннером в строке.
  chooseModel(model: string): void {
    const d = this.dish();
    if (!d || model === d.activeModel) {
      this.modelMenuOpen.set(false);
      return;
    }
    if (this.loading() || this.regenerating()) return;
    const isNew = !(d.variantModels ?? []).includes(model);
    this.switchError.set(null);
    this.regenError.set('');
    if (isNew) this.generatingModel.set(model);
    else this.modelMenuOpen.set(false);
    this.load(this.planId(), this.dishId(), model, 'select');
  }

  /** Кнопка модели на экране ошибки: собрать рецепт этой моделью (настройки не меняются). */
  retryWith(model: string): void {
    if (this.loading()) return;
    this.load(this.planId(), this.dishId(), model, 'select');
  }

  /** «Повторить» после неудачного выбора модели. */
  retrySwitch(): void {
    const e = this.switchError();
    if (e) this.chooseModel(e.model);
  }

  // «↻ Перегенерировать»: новый вариант рецепта той модели, что открыта сейчас, с учётом
  // обсуждения рецепта в чате. Бэк пишет вариант только после успеха — при ошибке старый
  // рецепт остаётся (и на экране, и в БД), ошибку показываем в футере.
  // «↻ Перегенерировать» сначала открывает окно «Что учесть?» (уточнение необязательно):
  // «соус на сливках», «без лука» → обязательная правка нового варианта.
  readonly regenAskOpen = signal(false);
  readonly regenNote = signal('');
  private lastRegenNote = '';

  askRegenerate(): void {
    if (!this.dish() || this.busy()) return;
    this.modelMenuOpen.set(false);
    this.regenNote.set('');
    this.regenAskOpen.set(true);
  }

  onRegenNote(e: Event): void {
    this.regenNote.set((e.target as HTMLTextAreaElement).value);
  }

  confirmRegenerate(): void {
    this.regenAskOpen.set(false);
    this.lastRegenNote = this.regenNote().trim();
    this.regenerate();
  }

  /** Сам запрос новой версии; «Повторить» после ошибки — с тем же уточнением. */
  regenerate(): void {
    const d = this.dish();
    if (!d || this.busy()) return;
    this.regenerating.set(true);
    this.regenError.set('');
    this.switchError.set(null);
    this.modelMenuOpen.set(false);
    const model = d.activeModel ?? ''; // пусто → бэк возьмёт настройку «Рецепты»
    this.api.dishDetails(this.planId(), this.dishId(), model, 'regenerate', this.lastRegenNote).subscribe({
      next: (nd) => {
        this.dish.set(nd);
        this.regenerating.set(false);
      },
      error: (err) => {
        this.regenError.set(aiFailText(err, model, 'новая версия не собрана, текущая на месте'));
        this.regenerating.set(false);
      },
    });
  }

  // «💬 Обсудить в чате»: беседа плана + бейдж «Обсуждение: <блюдо>» в композере.
  discuss(): void {
    const d = this.dish();
    if (!d || this.busy()) return;
    this.opening.set(true);
    this.api.getPlan(this.planId()).subscribe({
      next: (p) => {
        this.opening.set(false);
        if (!p.conversationId) return;
        this.store.startDiscuss({
          conversationId: p.conversationId,
          target: 'recipe',
          planId: p.id,
          dishId: d.id,
          name: d.name,
          model: d.activeModel,
        });
        this.router.navigate(['/chat']);
      },
      error: () => {
        this.opening.set(false);
        this.regenError.set('Не удалось открыть чат плана.');
      },
    });
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }
}
