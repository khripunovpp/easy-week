import { Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { EasyWeekApi, ModelPrice } from '../../services/api';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';

type PriceField = 'input' | 'cachedInput' | 'cacheWrite' | 'output' | 'per1kNeurons';

// Экран «Цены моделей» (/settings/prices): USD за 1M токенов по каждой модели. Цены у
// провайдеров меняются — правим тут; стоимость вызова считается бэком В МОМЕНТ вызова по
// текущей цене (метрика easyweek_ai_cost_usd_total → Grafana), история не пересчитывается.
@Component({
  selector: 'ew-settings-prices',
  imports: [RouterLink],
  templateUrl: './settings-prices.html',
  styleUrl: './settings-prices.scss',
})
export class SettingsPricesPage {
  private readonly api = inject(EasyWeekApi);

  readonly prices = signal<Record<string, ModelPrice> | null>(null);
  readonly status = signal<'idle' | 'saving' | 'saved' | 'error'>('idle');
  private saveTimer: ReturnType<typeof setTimeout> | null = null;

  readonly models: RecipeModel[] = ['deepseek', 'gemini', 'anthropic', 'cloudflare'];
  readonly fields: { key: PriceField; label: string }[] = [
    { key: 'input', label: 'Вход' },
    { key: 'cachedInput', label: 'Вход из кэша' },
    { key: 'cacheWrite', label: 'Запись в кэш' },
    { key: 'output', label: 'Выход' },
  ];

  constructor() {
    this.api.getPrices().subscribe({
      next: (r) => this.prices.set(r.prices),
      error: () => this.status.set('error'),
    });
  }

  label(m: string): string {
    return MODEL_LABELS[m as RecipeModel] ?? m;
  }

  value(m: string, f: PriceField): number | '' {
    const v = this.prices()?.[m]?.[f];
    return v ?? '';
  }

  /** Правка поля: обновляем локально и сохраняем с задержкой (пачкой, не на каждую цифру). */
  set(m: string, f: PriceField, raw: string): void {
    const num = Number(String(raw).replace(',', '.'));
    if (!Number.isFinite(num) || num < 0) return;
    const cur = this.prices();
    if (!cur) return;
    this.prices.set({ ...cur, [m]: { ...cur[m], [f]: num } });
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.status.set('saving');
    this.saveTimer = setTimeout(() => this.save(), 700);
  }

  private save(): void {
    const cur = this.prices();
    if (!cur) return;
    this.api.putPrices(cur).subscribe({
      next: (r) => {
        this.prices.set(r.prices);
        this.status.set('saved');
      },
      error: () => this.status.set('error'),
    });
  }
}
