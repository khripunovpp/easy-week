import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ChatStore } from '../../services/chat-store';
import { CookingLoader } from '../../shared/cooking-loader';

@Component({
  selector: 'ew-chat',
  imports: [FormsModule, RouterLink, CookingLoader],
  templateUrl: './chat.html',
  styleUrl: './chat.scss',
})
export class Chat {
  readonly store = inject(ChatStore);

  readonly menuOpen = signal(false);
  readonly countOptions = [3, 4, 5, 6, 7, 8];
  readonly cookingPhrases = [
    'Подбираю блюда',
    'Считаю тайминги',
    'Проверяю рецепты',
    'Раскладываю по неделе',
    'Собираю ингредиенты',
  ];
  readonly suggestions = ['Без свинины', 'На 2 порции', 'Побыстрее', 'Вегетарианские'];

  pastel(i: number): string {
    return `pastel-${i % 5}`;
  }

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }

  dishWord(n: number): string {
    const d10 = n % 10;
    const d100 = n % 100;
    if (d10 === 1 && d100 !== 11) return 'блюдо';
    if (d10 >= 2 && d10 <= 4 && (d100 < 12 || d100 > 14)) return 'блюда';
    return 'блюд';
  }

  toggleMenu(): void {
    this.menuOpen.update((v) => !v);
  }

  pickCount(n: number): void {
    this.store.setCount(n);
    this.menuOpen.set(false);
  }

  useSuggestion(text: string): void {
    this.store.draft.set(text);
  }
}
