import { Component, computed, input, output, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PlanSummary } from '../services/api';

// Селектор плана для «Покупок»/«Готовки»: показывает текущий план + статусы,
// позволяет сменить (событие pick) и перейти на страницу плана.
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

  readonly open = signal(false);
  readonly current = computed(
    () => this.plans().find((p) => p.id === this.selectedId()) ?? null,
  );

  toggle(): void {
    this.open.update((v) => !v);
  }
  choose(id: string): void {
    this.open.set(false);
    if (id !== this.selectedId()) this.pick.emit(id);
  }
  close(): void {
    this.open.set(false);
  }
  statusLabel(s: string): string {
    return s === 'accepted' ? 'принят' : s === 'rejected' ? 'отклонён' : 'черновик';
  }
}
