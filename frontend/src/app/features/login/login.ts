import { Component, ElementRef, afterNextRender, inject, input, signal, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { AuthService } from '../../services/auth';

// Экран входа по общему паролю. returnUrl — куда вернуться после входа (ставит гард/интерсептор).
@Component({
  selector: 'ew-login',
  templateUrl: './login.html',
  styleUrl: './login.scss',
})
export class LoginPage {
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  // Из query-параметра (withComponentInputBinding)
  readonly returnUrl = input<string>();

  readonly password = signal('');
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);

  private readonly field = viewChild<ElementRef<HTMLInputElement>>('pwd');

  constructor() {
    // autofocus-атрибут в SPA срабатывает только на первой загрузке — фокусируем сами.
    afterNextRender(() => this.field()?.nativeElement.focus());
  }

  async submit(event: Event): Promise<void> {
    event.preventDefault();
    const pwd = this.password();
    if (!pwd || this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    const err = await this.auth.login(pwd);
    this.busy.set(false);
    if (err) {
      this.error.set(err);
      return;
    }
    await this.router.navigateByUrl(this.safeReturnUrl());
  }

  // Только внутренние пути приложения (не //evil.com и не /login по кругу).
  private safeReturnUrl(): string {
    const url = this.returnUrl();
    if (!url || !url.startsWith('/') || url.startsWith('//') || url.startsWith('/login')) {
      return '/home';
    }
    return url;
  }
}
