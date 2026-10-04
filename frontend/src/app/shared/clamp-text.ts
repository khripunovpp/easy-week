import { afterNextRender, Component, DestroyRef, ElementRef, inject, input, signal, viewChild } from '@angular/core';

// Короткий текст в одну строку с многоточием + «развернуть / свернуть» (GUIDEBOOK «Свёрнутый текст»).
// Ссылка видна, только если текст не влез. Живёт и внутри ссылки (карточка блюда в чате) —
// поэтому переключатель — span role=button, клик не уводит по ссылке.
@Component({
  selector: 'ew-clamp',
  template: `
    <span class="clamp" [class.clamp--open]="open()">
      <span #txt class="clamp__text">{{ text() }}</span>
      @if (open() || overflow()) {
        <span
          class="link-btn clamp__toggle"
          role="button"
          tabindex="0"
          [attr.aria-expanded]="open()"
          (click)="toggle($event)"
          (keydown.enter)="toggle($event)"
          >{{ open() ? 'свернуть' : 'развернуть' }}</span>
      }
    </span>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .clamp {
      display: flex;
      align-items: baseline;
      gap: 6px;
      font-size: 13px;
      line-height: 1.35;
      color: var(--ink-2);
    }
    .clamp__text {
      flex: 1;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .clamp__toggle {
      flex-shrink: 0;
      font-size: 12.5px;
    }
    .clamp--open {
      display: block;
    }
    .clamp--open .clamp__text {
      white-space: normal;
    }
    .clamp--open .clamp__toggle {
      margin-left: 6px;
    }
  `,
})
export class ClampText {
  readonly text = input.required<string>();
  readonly open = signal(false);
  readonly overflow = signal(false);
  private readonly txt = viewChild.required<ElementRef<HTMLElement>>('txt');

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      const el = this.txt().nativeElement;
      // Не влез ли текст в строку — пересчёт при изменении ширины (поворот, ресайз).
      const measure = () => {
        if (!this.open()) this.overflow.set(el.scrollWidth > el.clientWidth + 1);
      };
      const ro = new ResizeObserver(measure);
      ro.observe(el);
      measure();
      destroyRef.onDestroy(() => ro.disconnect());
    });
  }

  toggle(e: Event): void {
    e.preventDefault();
    e.stopPropagation();
    this.open.update((v) => !v);
  }
}
