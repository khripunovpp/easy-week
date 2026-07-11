import { ChatMessage, Dish, WeekPlan } from '../models/plan.model';

// Мок недельного плана — имитирует JSON-ответ ассистента, пока нет бэкенда.
export const MOCK_PLAN: WeekPlan = {
  id: 'plan-1',
  title: '5 ужинов впрок',
  weekLabel: '14–20 июля',
  status: 'draft',
  dishes: [
    {
      id: 'dish-plov',
      name: 'Плов с говядиной',
      emoji: '🍲',
      servings: 4,
      prepMin: 20,
      cookMin: 60,
      tags: ['ужин', 'впрок'],
      storage: {
        vacuum: true,
        freeze: true,
        shelfLifeDays: 30,
        note: 'Разморозить в холодильнике, прогреть на сковороде под крышкой.',
      },
      tips: [
        'Обжарь мясо крупными кусками до корочки — так сочнее после заморозки.',
        'Не досаливай: после разморозки вкус концентрируется.',
      ],
      steps: [
        'Нарежь говядину крупными кубиками, лук — полукольцами, морковь — соломкой.',
        'Обжарь мясо до корочки, добавь лук и морковь, туши 10 минут.',
        'Всыпь промытый рис, залей горячей водой на 2 см выше риса.',
        'Готовь под крышкой на тихом огне 40 минут, дай настояться 10 минут.',
      ],
      ingredients: [
        { name: 'Говядина', qty: 600, unit: 'г', category: 'Мясо и птица' },
        { name: 'Рис басмати', qty: 400, unit: 'г', category: 'Бакалея' },
        { name: 'Морковь', qty: 300, unit: 'г', category: 'Овощи' },
        { name: 'Лук репчатый', qty: 200, unit: 'г', category: 'Овощи' },
        { name: 'Зира', qty: 1, unit: 'ст.л.', category: 'Специи' },
      ],
    },
    {
      id: 'dish-kotlety',
      name: 'Куриные котлеты',
      emoji: '🍗',
      servings: 6,
      prepMin: 25,
      cookMin: 20,
      tags: ['ужин', 'впрок'],
      storage: {
        vacuum: true,
        freeze: true,
        shelfLifeDays: 60,
        note: 'Замораживать сырыми в один слой, жарить не размораживая.',
      },
      tips: ['Добавь ложку ледяной воды в фарш — котлеты будут пышнее.'],
      steps: [
        'Пропусти филе через мясорубку с луком.',
        'Вмешай яйцо, размоченный хлеб, соль и перец.',
        'Сформируй котлеты, обжарь по 4 минуты с каждой стороны.',
      ],
      ingredients: [
        { name: 'Куриное филе', qty: 800, unit: 'г', category: 'Мясо и птица' },
        { name: 'Лук репчатый', qty: 150, unit: 'г', category: 'Овощи' },
        { name: 'Яйцо', qty: 1, unit: 'шт', category: 'Молочное' },
        { name: 'Батон', qty: 100, unit: 'г', category: 'Бакалея' },
      ],
    },
    {
      id: 'dish-ragu',
      name: 'Рагу с индейкой',
      emoji: '🥘',
      servings: 4,
      prepMin: 15,
      cookMin: 45,
      tags: ['ужин', 'впрок'],
      storage: {
        vacuum: true,
        freeze: true,
        shelfLifeDays: 40,
        note: 'Разогреть из заморозки на тихом огне, помешивая.',
      },
      tips: ['Овощи режь одинаковым кубиком — равномерно приготовятся.'],
      steps: [
        'Обжарь кубики индейки до румяности.',
        'Добавь кабачок, перец, морковь и томаты.',
        'Туши под крышкой 30 минут до мягкости.',
      ],
      ingredients: [
        { name: 'Филе индейки', qty: 500, unit: 'г', category: 'Мясо и птица' },
        { name: 'Кабачок', qty: 300, unit: 'г', category: 'Овощи' },
        { name: 'Перец болгарский', qty: 200, unit: 'г', category: 'Овощи' },
        { name: 'Томаты в собственном соку', qty: 400, unit: 'г', category: 'Бакалея' },
      ],
    },
    {
      id: 'dish-losos',
      name: 'Запечённый лосось',
      emoji: '🐟',
      servings: 2,
      prepMin: 10,
      cookMin: 25,
      tags: ['ужин'],
      storage: {
        vacuum: true,
        freeze: true,
        shelfLifeDays: 20,
        note: 'Лучше съесть свежим; в заморозке — до 20 дней.',
      },
      tips: ['Не пересуши: 12 минут при 200 °C достаточно для стейка 2 см.'],
      steps: [
        'Сбрызни стейки лимоном и оливковым маслом.',
        'Запеки 12–15 минут при 200 °C.',
      ],
      ingredients: [
        { name: 'Стейк лосося', qty: 400, unit: 'г', category: 'Рыба' },
        { name: 'Лимон', qty: 1, unit: 'шт', category: 'Овощи' },
        { name: 'Оливковое масло', qty: 2, unit: 'ст.л.', category: 'Бакалея' },
      ],
    },
    {
      id: 'dish-sup',
      name: 'Овощной суп-пюре',
      emoji: '🍜',
      servings: 5,
      prepMin: 15,
      cookMin: 30,
      tags: ['ужин', 'впрок'],
      storage: {
        vacuum: true,
        freeze: true,
        shelfLifeDays: 90,
        note: 'Замораживать порциями; разогревать не размораживая.',
      },
      tips: ['Пробей блендером горячим — текстура будет бархатной.'],
      steps: [
        'Обжарь лук, добавь тыкву, картофель и морковь.',
        'Залей бульоном, вари 20 минут до мягкости.',
        'Пробей блендером, посоли по вкусу.',
      ],
      ingredients: [
        { name: 'Тыква', qty: 500, unit: 'г', category: 'Овощи' },
        { name: 'Картофель', qty: 300, unit: 'г', category: 'Овощи' },
        { name: 'Морковь', qty: 200, unit: 'г', category: 'Овощи' },
        { name: 'Лук репчатый', qty: 100, unit: 'г', category: 'Овощи' },
      ],
    },
  ],
};

// Пул запасных блюд — имитирует «добавь ещё блюдо», пока нет бэкенда.
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

// Поиск блюда по id (пока данные из мока).
export function findDish(id: string): Dish | undefined {
  return [...MOCK_PLAN.dishes, ...EXTRA_DISHES].find((d) => d.id === id);
}

// История планов (мок) — для экрана «Планы».
export const MOCK_HISTORY: WeekPlan[] = [
  { ...MOCK_PLAN },
  {
    id: 'plan-prev-1',
    title: 'Рыбная неделя',
    weekLabel: '7–13 июля',
    status: 'accepted',
    dishes: MOCK_PLAN.dishes.slice(0, 4),
  },
  {
    id: 'plan-prev-2',
    title: 'Быстрые ужины',
    weekLabel: '30 июня – 6 июля',
    status: 'accepted',
    dishes: MOCK_PLAN.dishes.slice(0, 3),
  },
  {
    id: 'plan-prev-3',
    title: 'Вегетарианская',
    weekLabel: '23–29 июня',
    status: 'rejected',
    dishes: MOCK_PLAN.dishes.slice(2, 5),
  },
];

// Стартовая переписка чата (мок).
export const MOCK_MESSAGES: ChatMessage[] = [
  {
    id: 'm1',
    role: 'assistant',
    text: 'Привет! Составлю меню на неделю под заморозку. Сколько ужинов и есть ли ограничения?',
  },
  {
    id: 'm2',
    role: 'user',
    text: '5 ужинов, без свинины, готовлю впрок на 2–4 порции',
  },
  {
    id: 'm3',
    role: 'assistant',
    text: 'Готово — вот план на неделю. Всё можно завакуумировать и заморозить 👇',
    plan: MOCK_PLAN,
  },
];
