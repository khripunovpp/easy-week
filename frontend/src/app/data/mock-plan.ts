import { Dish } from '../models/plan.model';

// Запасной пул блюд для клиентского «Добавить блюдо» (визуально, до интеграции с AI).
export const EXTRA_DISHES: Dish[] = [
  {
    id: 'dish-golubcy',
    name: 'Ленивые голубцы',
    emoji: '🥬',
    servings: 5,
    prepMin: 20,
    cookMin: 40,
    tags: ['ужин', 'впрок'],
    storage: { vacuum: true, freeze: true, shelfLifeDays: 45, note: 'Замораживать порциями.' },
    tips: ['Капусту нашинкуй тонко — быстрее приготовится.'],
    steps: ['Смешай фарш, рис и капусту.', 'Сформируй, туши в соусе 40 минут.'],
    ingredients: [
      { name: 'Фарш говяжий', qty: 500, unit: 'г', category: 'Мясо и птица' },
      { name: 'Капуста', qty: 400, unit: 'г', category: 'Овощи' },
      { name: 'Рис', qty: 100, unit: 'г', category: 'Бакалея' },
    ],
  },
  {
    id: 'dish-perec',
    name: 'Фаршированный перец',
    emoji: '🫑',
    servings: 4,
    prepMin: 25,
    cookMin: 45,
    tags: ['ужин', 'впрок'],
    storage: { vacuum: true, freeze: true, shelfLifeDays: 50, note: 'Разогревать в соусе.' },
    tips: ['Перец бланшируй 3 минуты — легче фаршировать.'],
    steps: ['Смешай фарш с рисом.', 'Нафаршируй перцы, туши 45 минут.'],
    ingredients: [
      { name: 'Фарш куриный', qty: 500, unit: 'г', category: 'Мясо и птица' },
      { name: 'Перец болгарский', qty: 6, unit: 'шт', category: 'Овощи' },
      { name: 'Рис', qty: 120, unit: 'г', category: 'Бакалея' },
    ],
  },
  {
    id: 'dish-tefteli',
    name: 'Тефтели в томате',
    emoji: '🍅',
    servings: 5,
    prepMin: 20,
    cookMin: 35,
    tags: ['ужин', 'впрок'],
    storage: { vacuum: true, freeze: true, shelfLifeDays: 55, note: 'Морозить вместе с соусом.' },
    tips: ['Добавь в фарш тёртый лук для сочности.'],
    steps: ['Сформируй тефтели.', 'Обжарь и потуши в томатном соусе 30 минут.'],
    ingredients: [
      { name: 'Фарш говяжий', qty: 600, unit: 'г', category: 'Мясо и птица' },
      { name: 'Томаты в собственном соку', qty: 400, unit: 'г', category: 'Бакалея' },
      { name: 'Лук репчатый', qty: 150, unit: 'г', category: 'Овощи' },
    ],
  },
];
