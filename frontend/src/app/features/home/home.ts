import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

@Component({
  selector: 'ew-home',
  imports: [RouterLink],
  templateUrl: './home.html',
  styleUrl: './home.scss',
})
export class Home {
  readonly steps = [
    { emoji: '💬', title: 'Опишите неделю', text: 'Сколько ужинов, порции, что исключить' },
    { emoji: '🧑‍🍳', title: 'Получите план', text: 'Блюда, тайминги, рецепты и хранение' },
    { emoji: '🛒', title: 'Список покупок', text: 'Соберётся сам — и всё под заморозку' },
  ];
}
