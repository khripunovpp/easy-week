import { Component, input } from '@angular/core';

@Component({
  selector: 'ew-stub',
  template: `
    <div class="stub">
      <span class="stub__emoji">{{ emoji() }}</span>
      <h2>{{ title() }}</h2>
      <p class="muted">Скоро здесь появится этот экран.</p>
    </div>
  `,
  styles: [
    `
      .stub {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 8px;
        padding: 40px;
        text-align: center;
      }
      .stub__emoji {
        font-size: 46px;
      }
      h2 {
        font-size: 22px;
      }
    `,
  ],
})
export class Stub {
  readonly title = input('Экран');
  readonly emoji = input('🧑‍🍳');
}
