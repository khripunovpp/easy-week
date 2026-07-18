import { Location } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dish } from '../../models/plan.model';
import { EasyWeekApi } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-dish',
  imports: [RouterLink, CookingLoader],
  templateUrl: './dish.html',
  styleUrl: './dish.scss',
})
export class DishPage {
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
      this.router.navigate(['/plan', this.planId()]);
    }
  }

  readonly dish = signal<Dish | null>(null);
  readonly loading = signal(true);
  readonly failed = signal(false);
  readonly errorMsg = signal('');
  readonly modelMenuOpen = signal(false);

  // Модели, для которых варианта рецепта ещё нет (для ⟳-выпадашки). Пусто → ⟳ прячем.
  readonly remainingModels = computed<RecipeModel[]>(() => {
    const have = new Set(this.dish()?.variantModels ?? []);
    return ALL_MODELS.filter((m) => !have.has(m));
  });

  constructor() {
    // Догружаем блюдо (с ленивой генерацией шагов на бэкенде) при смене маршрута.
    effect(() => {
      const pid = this.planId();
      const did = this.dishId();
      if (!pid || !did) return;
      // Если пришли с ?model= (напр. из готовки — модель плана) — открываем именно её вариант
      // (сгенерится, если ещё нет); иначе — активный/дефолтный вариант.
      const m = this.model();
      if (m) this.load(pid, did, m, 'select');
      else this.load(pid, did, this.store.recipeModel(), 'open');
    });
  }

  private load(pid: string, did: string, model: string, action: 'open' | 'select'): void {
    this.loading.set(true);
    this.failed.set(false);
    this.api.dishDetails(pid, did, model, action).subscribe({
      next: (d) => {
        this.dish.set(d);
        this.loading.set(false);
      },
      error: (err) => {
        // 429 (дневной лимит) и прочие ошибки — показываем текст с бэка, если есть
        this.errorMsg.set(err?.error?.detail ?? '');
        this.failed.set(true);
        this.loading.set(false);
      },
    });
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  // Клик по вкладке модели или выбор в ⟳-выпадашке: делаем вариант активным
  // (генерим на бэке, если его ещё нет). Активная модель — no-op.
  chooseModel(model: string): void {
    this.modelMenuOpen.set(false);
    const d = this.dish();
    if (!d || this.loading() || model === d.activeModel) return;
    this.load(this.planId(), this.dishId(), model, 'select');
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }
}
