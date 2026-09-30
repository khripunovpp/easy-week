import { Component, computed, inject, input } from '@angular/core';
import { ModelSettings, splitRef } from '../services/model-settings';
import { MODEL_LABELS, RecipeModel } from '../services/preferences';

// Название модели в выпадашках: провайдер (или своя подпись) + конкретная модель мелкой строкой под ним
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
  // Ключ провайдера или ссылка «провайдер:id» (конкретная модель).
  readonly model = input.required<string>();
  // Своя подпись вместо имени провайдера (пункт группы в настройках: «Claude Sonnet 5.5»).
  readonly labelOverride = input('');
  readonly label = computed(() => {
    const p = splitRef(this.model())[0] as RecipeModel;
    return this.labelOverride() || (MODEL_LABELS[p] ?? p);
  });
  // Своя вторая строка (пункт группы: «id · пометка»); пусто — конкретная модель за ключом.
  readonly sub = input('');
  readonly id = computed(() => {
    if (this.sub()) return this.sub();
    const id = this.settings.modelId(this.model());
    return id.startsWith('@cf/') ? (id.split('/').pop() ?? id) : id; // Cloudflare — без «@cf/…/»
  });
}
