import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { EasyWeekApi, LimitsStatus, ModelTask } from '../../services/api';
import { ModelSettings } from '../../services/model-settings';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';

// Экран «Модели по умолчанию» (/settings/models, под-экран профиля): модель для каждой задачи.
// Хранится на сервере (общая для всех устройств семьи); страницы стартуют с неё, но могут
// выбрать другую локально.
@Component({
  selector: 'ew-settings-models',
  imports: [RouterLink],
  templateUrl: './settings-models.html',
  styleUrl: './settings-models.scss',
})
export class SettingsModelsPage {
  readonly models = inject(ModelSettings);
  private readonly api = inject(EasyWeekApi);

  // Остаток дневного лимита Claude (планы/рецепты) — если Claude выбран хоть для одной задачи.
  readonly limits = signal<LimitsStatus | null>(null);
  readonly usesClaude = computed(() => Object.values(this.models.models()).includes('anthropic'));
  // Какая выпадашка открыта (одна за раз).
  readonly openTask = signal<ModelTask | null>(null);

  readonly allModels = ALL_MODELS;

  // Строки: задача → подпись и что в неё входит.
  readonly taskRows: { task: ModelTask; label: string; hint: string }[] = [
    { task: 'chat', label: 'Чат и план', hint: 'план недели, правки, обсуждение' },
    { task: 'recipe', label: 'Рецепты', hint: 'рецепт блюда, догенерация для PDF' },
    { task: 'shopping', label: 'Список покупок', hint: 'сведение и чистка списка' },
    { task: 'cooking', label: 'План готовки', hint: 'порядок готовки всех блюд' },
  ];

  constructor() {
    this.models.refresh(); // свежие значения (могли поменять с другого устройства)
    this.api.limits().subscribe({ next: (l) => this.limits.set(l) });
  }

  modelLabel(key: RecipeModel): string {
    return MODEL_LABELS[key] ?? key;
  }

  toggleTask(task: ModelTask): void {
    this.openTask.update((cur) => (cur === task ? null : task));
  }

  pickModel(task: ModelTask, model: RecipeModel): void {
    this.openTask.set(null);
    if (this.models.models()[task] !== model) this.models.set(task, model);
  }
}
