import { afterNextRender, Component, ElementRef, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { EasyWeekApi, MessageSearchHit } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { providerToModel, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { dishColorClass } from '../../shared/dish-color';
import { renderMarkdown } from '../../shared/markdown';
import { Vote } from '../../shared/vote';

@Component({
  selector: 'ew-chat',
  imports: [FormsModule, RouterLink, CookingLoader, Vote],
  templateUrl: './chat.html',
  styleUrl: './chat.scss',
})
export class Chat {
  readonly store = inject(ChatStore);
  private readonly router = inject(Router);
  private readonly api = inject(EasyWeekApi);

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

  readonly menuOpen = signal(false);
  readonly modelMenuOpen = signal(false);
  readonly countOptions = [2, 3, 4, 5, 6, 7, 8];
  readonly modelOptions: { value: RecipeModel; label: string }[] = [
    { value: 'deepseek', label: 'DeepSeek' },
    { value: 'gemini', label: 'Gemini' },
    { value: 'anthropic', label: 'Claude' },
    { value: 'cloudflare', label: 'Cloudflare' },
  ];

  private readonly streamEl = viewChild<ElementRef<HTMLElement>>('stream');
  private lastBump = 0;

  constructor() {
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

  // --- Long-press по реплике бота → тултип с оценкой 👍/👎 ---
  readonly tipId = signal<string | null>(null); // serverId сообщения с открытым тултипом
  private pressTimer: ReturnType<typeof setTimeout> | undefined;

  canVote(m: { role: string; serverId?: string }): boolean {
    return m.role === 'assistant' && !!m.serverId;
  }
  convId(): string {
    return this.store.conversationId ?? '';
  }
  pressStart(m: { role: string; serverId?: string }): void {
    if (!this.canVote(m)) return;
    clearTimeout(this.pressTimer);
    this.pressTimer = setTimeout(() => this.tipId.set(m.serverId!), 420);
  }
  pressEnd(): void {
    clearTimeout(this.pressTimer);
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

  // Лейбл бейджа действия в композере (замена конкретного блюда / добавление).
  pendingLabel(): string {
    const p = this.store.pending();
    if (!p) return '';
    return p.kind === 'replace' ? `Замена: ${p.name}` : 'Добавить блюдо';
  }

  composerPlaceholder(): string {
    const p = this.store.pending();
    if (p?.kind === 'replace') return 'Пожелания к замене (необязательно)…';
    if (p?.kind === 'add') return 'Какое блюдо добавить?…';
    return 'Опишите, что приготовить…';
  }

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

  toggleMenu(): void {
    this.menuOpen.update((v) => !v);
  }

  pickCount(n: number): void {
    this.store.setCount(n);
    this.menuOpen.set(false);
  }

  modelLabel(value: RecipeModel): string {
    return this.modelOptions.find((o) => o.value === value)?.label ?? value;
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  pickModel(m: RecipeModel): void {
    this.store.setModel(m);
    this.modelMenuOpen.set(false);
  }
}
