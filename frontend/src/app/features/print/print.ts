import { Location } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { WeekPlan } from '../../models/plan.model';
import { EasyWeekApi, ShoppingGroup } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-print',
  imports: [CookingLoader],
  templateUrl: './print.html',
  styleUrl: './print.scss',
})
export class PrintPage {
  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly location = inject(Location);

  readonly planId = input<string>('');
  readonly parts = input<string>('all'); // recipes | shopping | all (query-параметр)

  readonly plan = signal<WeekPlan | null>(null);
  readonly shopping = signal<ShoppingGroup[]>([]);
  readonly loading = signal(true);

  readonly showRecipes = signal(true);
  readonly showShopping = signal(true);

  readonly ready = computed(() => !this.loading() && this.plan() !== null);

  constructor() {
    effect(() => {
      const pid = this.planId();
      const parts = this.parts();
      if (!pid) return;
      this.showRecipes.set(parts !== 'shopping');
      this.showShopping.set(parts !== 'recipes');
      this.loadAll(pid);
    });
  }

  private loadAll(planId: string): void {
    this.loading.set(true);
    this.api.fullPlan(planId, this.store.recipeModel()).subscribe({
      next: (plan) => {
        this.plan.set(plan);
        this.api.shoppingList(planId).subscribe({
          next: (g) => {
            this.shopping.set(g);
            this.loading.set(false);
          },
          error: () => this.loading.set(false),
        });
      },
      error: () => this.loading.set(false),
    });
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  readonly busy = signal(false);

  back(): void {
    this.location.back();
  }

  // Экспорт PDF одной кнопкой. В защищённом контексте (HTTPS) — системный шэр самим
  // ФАЙЛОМ (Telegram и т.п.). Иначе (LAN по HTTP — Web Share недоступен) открываем
  // настоящий PDF-URL сервера: без blob:-ссылки, которую iOS шэрит вместо файла.
  async exportPdf(): Promise<void> {
    const plan = this.plan();
    if (!plan) return;
    if (typeof navigator.canShare === 'function') {
      const blob = await this.fetchPdf();
      if (blob) {
        const file = new File([blob], this.filename(), { type: 'application/pdf' });
        if (navigator.canShare({ files: [file] })) {
          try {
            await navigator.share({ files: [file], title: this.shareTitle() });
          } catch {
            /* пользователь отменил шэр */
          }
          return;
        }
      }
    }
    window.open(this.pdfUrl(plan.id), '_blank');
  }

  private pdfUrl(planId: string): string {
    const params = new URLSearchParams({
      recipes: String(this.showRecipes()),
      shopping: String(this.showShopping()),
    });
    return `/api/plans/${planId}/pdf?${params}`;
  }

  // PDF генерит бэкенд (fpdf2 + DejaVu): надёжная кириллица, лёгкий фронт.
  private async fetchPdf(): Promise<Blob | null> {
    const plan = this.plan();
    if (!plan) return null;
    this.busy.set(true);
    try {
      const resp = await fetch(this.pdfUrl(plan.id));
      return resp.ok ? await resp.blob() : null;
    } catch {
      return null;
    } finally {
      this.busy.set(false);
    }
  }

  private filename(): string {
    const name = (this.plan()?.title ?? 'меню').replace(/[^0-9a-zа-яё \-—]/gi, '').trim();
    return `Easy Week — ${name || 'меню'}.pdf`;
  }

  private shareTitle(): string {
    const p = this.plan();
    return p ? `Меню на ${p.weekLabel} — ${p.title}` : 'Меню на неделю';
  }
}
