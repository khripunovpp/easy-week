import { Component, computed, input, output, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PlanSummary } from '../services/api';

// Пикер плана для «Покупок»/«Готовки»: выглядит как заголовок (название плана + статус под ним),
// справа — контрол открытия списка. В списке — только принятые и черновики (порядок = дата
// создания, приходит с бэка), поиск, первые 10, остальное за «Загрузить ещё».
@Component({
  selector: 'ew-plan-picker',
  imports: [RouterLink],
  templateUrl: './plan-picker.html',
  styleUrl: './plan-picker.scss',
})
export class PlanPicker {
  readonly plans = input<PlanSummary[]>([]);
  readonly selectedId = input<string>('');
  readonly pick = output<string>();

  private readonly PAGE = 10;
  readonly open = signal(false);
  readonly query = signal('');
  readonly limit = signal(this.PAGE);

  readonly current = computed(() => this.plans().find((p) => p.id === this.selectedId()) ?? null);

  // Только принятые и черновики; порядок сохраняем (created_at desc с бэка).
  readonly listable = computed(() =>
    this.plans().filter((p) => p.status === 'accepted' || p.status === 'draft'),
  );
  readonly filtered = computed(() => {
    const q = this.query().trim().toLowerCase();
    const base = this.listable();
    return q ? base.filter((p) => p.title.toLowerCase().includes(q)) : base;
  });
  readonly visible = computed(() => this.filtered().slice(0, this.limit()));
  readonly hasMore = computed(() => this.filtered().length > this.limit());

  toggle(): void {
    const next = !this.open();
    this.open.set(next);
    if (next) {
      this.query.set('');
      this.limit.set(this.PAGE);
    }
  }
  close(): void {
    this.open.set(false);
  }
  onQuery(v: string): void {
    this.query.set(v);
    this.limit.set(this.PAGE);
  }
  loadMore(): void {
    this.limit.update((n) => n + this.PAGE);
  }
  choose(id: string): void {
    this.close();
    if (id !== this.selectedId()) this.pick.emit(id);
  }
  statusLabel(s: string): string {
    return s === 'accepted' ? 'принят' : s === 'rejected' ? 'отклонён' : 'черновик';
  }
}
