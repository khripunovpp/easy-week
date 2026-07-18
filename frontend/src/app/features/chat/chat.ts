import { afterNextRender, Component, ElementRef, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { ChatStore } from '../../services/chat-store';
import { RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { renderMarkdown } from '../../shared/markdown';

@Component({
  selector: 'ew-chat',
  imports: [FormsModule, RouterLink, CookingLoader],
  templateUrl: './chat.html',
  styleUrl: './chat.scss',
})
export class Chat {
  readonly store = inject(ChatStore);
  private readonly router = inject(Router);

  readonly menuOpen = signal(false);
  readonly modelMenuOpen = signal(false);
  readonly countOptions = [2, 3, 4, 5, 6, 7, 8];
  readonly modelOptions: { value: RecipeModel; label: string }[] = [
    { value: 'deepseek', label: 'DeepSeek' },
    { value: 'gemini', label: 'Gemini' },
    { value: 'anthropic', label: 'Claude' },
    { value: 'cloudflare', label: 'Cloudflare' },
  ];
  readonly suggestions = ['Без свинины', 'На 2 порции', 'Побыстрее', 'Вегетарианские'];

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

  // Реплики бота приходят в markdown — рендерим в безопасный HTML.
  renderMarkdown(md: string): string {
    return renderMarkdown(md);
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
    return `pastel-${i % 5}`;
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

  useSuggestion(text: string): void {
    this.store.draft.set(text);
  }
}
