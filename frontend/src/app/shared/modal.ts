import { Component, DestroyRef, ElementRef, afterNextRender, inject, input, output, signal, viewChild } from '@angular/core';

// Модалка (GUIDEBOOK → «Модалка»): нативный <dialog class="modal"> + showModal() — top layer
// поверх всего (тултипы, фикс-шапки, таб-бар), фокус внутри, Esc закрывает браузер.
// Открыта, пока отрисована: `@if (x) { <ew-modal (closed)="x = null">…</ew-modal> }`.
// Контент проецируется в .modal__card. Высота — по visualViewport: на iPhone клавиатура
// сжимает его, а не окно, и без этого карточка уезжает под клавиатуру.
@Component({
  selector: 'ew-modal',
  template: `
    <dialog
      #dlg
      class="modal"
      [attr.aria-label]="label() || null"
      [style.top.px]="vp().top"
      [style.height.px]="vp().height"
      (click)="onBackdrop($event)"
      (cancel)="onCancel($event)">
      <div class="modal__card">
        <ng-content />
      </div>
    </dialog>
  `,
  styles: `
    .modal {
      bottom: auto;
    }
  `,
})
export class Modal {
  /** Подпись для скринридера (если в карточке нет явного заголовка). */
  readonly label = input('');
  /** Закрыть просят: тап по затемнению или Esc. Родитель убирает модалку из шаблона. */
  readonly closed = output<void>();

  private readonly dlg = viewChild.required<ElementRef<HTMLDialogElement>>('dlg');
  readonly vp = signal({ top: 0, height: window.innerHeight });

  constructor() {
    this.fit();
    afterNextRender(() => {
      const el = this.dlg().nativeElement;
      if (el.showModal) el.showModal();
      else el.setAttribute('open', ''); // без API — останется fixed-оверлеем
    });
    const vv = window.visualViewport;
    const fit = () => this.fit();
    vv?.addEventListener('resize', fit);
    vv?.addEventListener('scroll', fit);
    window.addEventListener('resize', fit);
    inject(DestroyRef).onDestroy(() => {
      vv?.removeEventListener('resize', fit);
      vv?.removeEventListener('scroll', fit);
      window.removeEventListener('resize', fit);
    });
  }

  private fit(): void {
    const vv = window.visualViewport;
    this.vp.set(vv ? { top: vv.offsetTop, height: vv.height } : { top: 0, height: window.innerHeight });
  }

  /** Тап по затемнению (сам <dialog>, не карточка). */
  onBackdrop(e: MouseEvent): void {
    if (e.target === e.currentTarget) this.closed.emit();
  }

  /** Esc: не даём браузеру закрыть диалог самому — состояние держит родитель. */
  onCancel(e: Event): void {
    e.preventDefault();
    this.closed.emit();
  }
}
