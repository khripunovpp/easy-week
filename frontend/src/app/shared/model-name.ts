import { Component, computed, inject, input } from '@angular/core';
import { ModelSettings } from '../services/model-settings';
import { MODEL_LABELS, RecipeModel } from '../services/preferences';

// Название модели в выпадашках: провайдер + конкретная модель мелкой строкой под ним
// («Gemini» / «gemini-flash-latest → gemini-…-flash»). Конкретные имена — с сервера
// (/api/settings → modelNames), поэтому видно, какая модель реально отвечает.
@Component({
  selector: 'ew-model-name',
  template: `
    <span class="mname">
      <span class="mname__label">{{ label() }}</span>
      @if (id()) {
        <span class="mname__id">{{ id() }}</span>
      }
    </span>
  `,
  styles: `
    :host {
      display: contents;
    }
    .mname {
      display: flex;
      flex-direction: column;
      gap: 1px;
      min-width: 0;
      text-align: left;
    }
    .mname__id {
      font-size: 11.5px;
      font-weight: 500;
      line-height: 1.25;
      color: var(--ink-3);
      /* переносим по дефисам/пробелам, посреди слова — только если не влезает совсем */
      overflow-wrap: break-word;
    }
  `,
})
export class ModelName {
  private readonly settings = inject(ModelSettings);
  readonly model = input.required<string>();
  readonly label = computed(() => MODEL_LABELS[this.model() as RecipeModel] ?? this.model());
  readonly id = computed(() => this.settings.modelId(this.model()));
}
