import { afterNextRender, Component, computed, effect, ElementRef, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { DiscussRef } from '../../models/plan.model';
import { EasyWeekApi, MessageSearchHit } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { ModelSettings } from '../../services/model-settings';
import { MODEL_LABELS, RecipeModel, providerToModel } from '../../services/preferences';
import { ClampText } from '../../shared/clamp-text';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { leftoverChips } from '../../shared/leftovers';
import { renderMarkdown } from '../../shared/markdown';
import { StorageBadge } from '../../shared/storage';
import { Vote } from '../../shared/vote';
import { ModelName } from '../../shared/model-name';
import { PlanWizardCard } from './plan-wizard';

@Component({
  selector: 'ew-chat',
  imports: [FormsModule, RouterLink, CookingLoader, Vote, ModelName, PlanWizardCard, ClampText, StorageBadge],
  templateUrl: './chat.html',
  styleUrl: './chat.scss',
})
export class Chat {
  readonly store = inject(ChatStore);
  private readonly router = inject(Router);
  private readonly api = inject(EasyWeekApi);
  // Дефолт модели чата — из общих настроек; подтянуть, если стор стартовал до входа.
  private readonly modelSettings = inject(ModelSettings);

  // Поиск по сообщениям всех бесед. searchOpen — режим поиска (лента скрыта).
  // fromSearch — текущая беседа открыта из результатов (показываем «назад»).
  readonly searchOpen = signal(false);
  readonly searchQuery = signal('');
  readonly searchResults = signal<MessageSearchHit[]>([]);
  readonly searchLoading = signal(false);
  readonly fromSearch = signal(false);
  private searchTimer: ReturnType<typeof setTimeout> | undefined;

  toggleSearch(): void {
    this.searchOpen.update((v) => !v);
  }
  backToSearch(): void {
    this.searchOpen.set(true);
  }
  onSearchInput(v: string): void {
    this.searchQuery.set(v);
    clearTimeout(this.searchTimer);
    const q = v.trim();
    if (!q) {
      this.searchResults.set([]);
      this.searchLoading.set(false);
      return;
    }
    this.searchLoading.set(true);
    this.searchTimer = setTimeout(() => {
      this.api.searchMessages(q).subscribe({
        next: (r) => {
          this.searchResults.set(r);
          this.searchLoading.set(false);
        },
        error: () => {
          this.searchResults.set([]);
          this.searchLoading.set(false);
        },
      });
    }, 220);
  }
  openHit(hit: MessageSearchHit): void {
    this.store.loadConversation(hit.conversationId);
    this.searchOpen.set(false);
    this.fromSearch.set(true);
  }
  roleLabel(role: string): string {
    return role === 'user' ? 'Вы' : 'Бот';
  }

  readonly modelMenuOpen = signal(false);
  // Быстрый выбор плана — в новом чате, пока ничего не отправили и не идёт генерация.
  readonly wizardOn = computed(() => this.store.fresh() && !this.store.loading() && !this.store.pending());
  // Модели, которые можно выбрать для чата/плана — по карте задач с сервера.
  readonly modelOptions = this.modelSettings.modelsForSignal('chat');

  private readonly streamEl = viewChild<ElementRef<HTMLElement>>('stream');
  private readonly composerInput = viewChild<ElementRef<HTMLTextAreaElement>>('composerInput');
  private lastBump = 0;

  /** Поле ввода выросло больше одной строки — row скругляется мягче (см. .composer__row--multi). */
  readonly composerMulti = signal(false);

  constructor() {
    this.modelSettings.ensureLoaded();
    // Автовысота поля: на каждое изменение черновика (ввод, очистка после отправки,
    // смена чата) пересчитываем высоту. Потолок в 3 строки — max-height в chat.scss.
    effect(() => {
      this.store.draft();
      const el = this.composerInput()?.nativeElement;
      if (!el) return;
      // ngModel пишет значение в DOM через промис — меряем в следующем кадре, когда оно уже там.
      requestAnimationFrame(() => {
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight}px`;
        // 44px = одна строка (22px) + вертикальные паддинги (2 × 11px)
        this.composerMulti.set(el.scrollHeight > 44);
      });
    });
    // При входе в чат — прижимаем ленту к низу, чтобы сразу видеть последние сообщения.
    afterNextRender(() => this.scrollToBottom());
    // Пока идёт генерация — держим ленту прижатой к низу, чтобы новые блюда
    // и лоадер внутри карточки не уходили под композер.
    effect(() => {
      this.store.messages();
      this.store.streamingMsgId();
      const bump = this.store.scrollBump();
      // Пока идёт генерация — держим низ; плюс явный «тик» после правки (новая карточка внизу).
      if (!this.store.loading() && bump === this.lastBump) return;
      this.lastBump = bump;
      this.scrollToBottom();
    });
  }

  // Показывать кнопку «вниз», когда лента прокручена не до конца.
  readonly showScrollDown = signal(false);

  // Реплики бота приходят в markdown — рендерим в безопасный HTML.
  renderMarkdown(md: string): string {
    return renderMarkdown(md);
  }

  // --- Кнопка ⋮ у реплики бота → тултип с оценкой 👍/👎 ---
  readonly tipId = signal<string | null>(null); // serverId сообщения с открытым тултипом

  canVote(m: { role: string; serverId?: string }): boolean {
    return m.role === 'assistant' && !!m.serverId;
  }
  convId(): string {
    return this.store.conversationId ?? '';
  }
  // --- Кнопка «скопировать ID чата» в шапке (чтобы сослаться на беседу при разборе логов) ---
  readonly idCopied = signal(false);
  private copiedTimer: ReturnType<typeof setTimeout> | undefined;

  copyChatId(): void {
    const id = this.convId();
    if (!id) return;
    const done = () => {
      this.idCopied.set(true);
      clearTimeout(this.copiedTimer);
      this.copiedTimer = setTimeout(() => this.idCopied.set(false), 1500);
    };
    // Clipboard API есть только в secure context (Funnel-https); по LAN-http — фолбэк.
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(id).then(done, () => this.copyFallback(id) && done());
    } else if (this.copyFallback(id)) {
      done();
    }
  }

  private copyFallback(text: string): boolean {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length); // iOS не выделяет по select()
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }

  toggleTip(m: { serverId?: string }): void {
    this.tipId.update((cur) => (cur === m.serverId ? null : (m.serverId ?? null)));
  }
  closeTip(): void {
    this.tipId.set(null);
  }

  onStreamScroll(): void {
    const el = this.streamEl()?.nativeElement;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    this.showScrollDown.set(dist > 120);
  }

  /** Enter — отправить; Shift+Enter — перенос строки; во время IME-набора Enter не трогаем. */
  onComposerEnter(e: KeyboardEvent): void {
    if (e.shiftKey || e.isComposing) return;
    e.preventDefault();
    this.store.send();
  }

  /** Крестик в композере: очищаем черновик и оставляем фокус в поле, чтобы сразу печатать. */
  clearDraft(): void {
    this.store.draft.set('');
    this.composerInput()?.nativeElement.focus();
  }

  scrollDown(): void {
    const el = this.streamEl()?.nativeElement;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }

  private scrollToBottom(): void {
    const el = this.streamEl()?.nativeElement;
    if (el) requestAnimationFrame(() => (el.scrollTop = el.scrollHeight));
  }

  isStreaming(msgId: string): boolean {
    return this.store.streamingMsgId() === msgId;
  }

  // Принять план → перейти на его страницу; отклонить → остаёмся в чате.
  accept(msgId: string, planId: string): void {
    this.store.setPlanStatus(msgId, planId, 'accepted', () =>
      this.router.navigate(['/plan', planId]),
    );
  }
  reject(msgId: string, planId: string): void {
    this.store.setPlanStatus(msgId, planId, 'rejected');
  }

  // Лейбл бейджа действия в композере (замена блюда / добавление / обсуждение цели).
  pendingLabel(): string {
    const p = this.store.pending();
    if (!p) return '';
    if (p.kind === 'discuss') return `Обсуждение: ${p.name}`;
    return p.kind === 'replace' ? `Замена: ${p.name}` : 'Добавить блюдо';
  }

  composerPlaceholder(): string {
    if (this.wizardOn()) {
      return this.store.wizard.choice() ? 'Уточнение (важнее выбора)…' : 'Или просто напишите, чего хочется…';
    }
    const p = this.store.pending();
    if (p?.kind === 'replace') return 'Пожелания к замене (необязательно)…';
    if (p?.kind === 'add') return 'Какое блюдо добавить?…';
    if (p?.kind === 'discuss') {
      if (p.target === 'cooking') return 'Спросите про план готовки…';
      if (p.target === 'shopping') return 'Спросите про список покупок…';
      return 'Спросите про рецепт…';
    }
    return 'Опишите, что приготовить…';
  }

  // Ссылка под ответом обсуждения — обратно на обсуждаемую цель.
  discussLink(ref: DiscussRef): unknown[] {
    if (ref.target === 'recipe' && ref.dishId) return ['/plan', ref.planId, 'dish', ref.dishId];
    return [ref.target === 'cooking' ? '/cooking' : '/shopping', ref.planId];
  }
  discussLinkLabel(ref: DiscussRef): string {
    if (ref.target === 'cooking') return 'Открыть план готовки →';
    if (ref.target === 'shopping') return 'Открыть покупки →';
    return 'Открыть рецепт →';
  }

  readonly leftoverChips = leftoverChips; // строка «🧺 Остатки» плана

  pastel(i: number): string {
    return dishColorClass(i); // цвет блюда по индексу (единая палитра)
  }

  providerKey(provider: string): string {
    return providerToModel(provider); // человекочитаемый provider → ключ модели для оценки
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  dishWord(n: number): string {
    const d10 = n % 10;
    const d100 = n % 100;
    if (d10 === 1 && d100 !== 11) return 'блюдо';
    if (d10 >= 2 && d10 <= 4 && (d100 < 12 || d100 > 14)) return 'блюда';
    return 'блюд';
  }

  newChat(): void {
    this.fromSearch.set(false);
    this.store.newChat();
  }


  modelLabel(value: RecipeModel): string {
    return MODEL_LABELS[value] ?? value;
  }

  /** Какой конкретной моделью ответит провайдер в чате (модель задачи «Чат и план», если тот же). */
  optRef(value: RecipeModel): string {
    return this.modelSettings.refFor('chat', value);
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  pickModel(m: RecipeModel): void {
    this.store.setModel(m);
    this.modelMenuOpen.set(false);
  }
}
