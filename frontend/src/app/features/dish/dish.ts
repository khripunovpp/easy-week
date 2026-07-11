import { Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { findDish } from '../../data/mock-plan';

@Component({
  selector: 'ew-dish',
  imports: [RouterLink],
  templateUrl: './dish.html',
  styleUrl: './dish.scss',
})
export class DishPage {
  readonly id = input<string>('');
  readonly dish = computed(() => findDish(this.id()));

  totalTime(prep: number, cook: number): number {
    return prep + cook;
  }
}
