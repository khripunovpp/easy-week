import { Injectable, computed, inject, signal } from '@angular/core';
import { EasyWeekApi } from '../services/api';

// Быстрый выбор в новом чате (GUIDEBOOK → «Быстрый выбор плана»): шаги-карточки вместо текста.
// Ответы собираются в строку запроса («Ужины · 5 блюд · до 30 минут · основа: птица, рыба»),
// которая уходит в чат обычным сообщением; дописанный текст идёт отдельной строкой
// «Уточнение (важнее выбора выше): …» — текст пользователя приоритетнее карточек (напр. выбрал
// «рыба», а написал «только сёмга» или «без рыбы»). Выбор разовый: в профиль вкусов не пишется
// (формулировки без слов «люблю/нравится» — экстрактор предпочтений их даже не увидит).

export interface WizOption {
  id: string;
  label: string;
  phrase: string; // во фразу запроса; пусто — «без разницы», во фразу не идёт
}

export interface WizStep {
  id: string;
  title: string;
  multi?: boolean; // можно несколько (иначе тап сразу ведёт дальше)
  options: WizOption[];
  prefix?: string; // для multi: «основа: » + выбранные через запятую
  extra?: boolean; // под «Ещё вопросы»
  showIf?: (a: Answers) => boolean;
}

export type Answers = Record<string, string[]>;

const o = (id: string, label: string, phrase = label.toLowerCase()): WizOption => ({ id, label, phrase });

export const WIZARD_STEPS: WizStep[] = [
  {
    id: 'meals', title: 'Для каких приёмов пищи?', multi: true, prefix: '',
    options: [o('breakfast', 'Завтраки'), o('lunch', 'Обеды'), o('dinner', 'Ужины'), o('snack', 'Перекусы')],
  },
  {
    id: 'count', title: 'Сколько блюд?',
    options: [o('3', '3 блюда', ''), o('5', '5 блюд', ''), o('7', '7 блюд', '')], // число — отдельно
  },
  {
    id: 'time', title: 'Сколько времени у плиты на блюдо?',
    options: [o('fast', 'До 30 минут', 'до 30 минут на блюдо'), o('hour', 'До часа', 'до часа на блюдо'), o('long', 'Можно долго', 'можно долго готовить')],
  },
  {
    id: 'satiety', title: 'Насколько сытно?',
    options: [o('light', 'Полегче', 'полегче'), o('normal', 'Обычно', ''), o('heavy', 'Посытнее', 'посытнее')],
  },
  {
    id: 'base', title: 'Что в основе?', multi: true, prefix: 'основа: ',
    options: [o('meat', 'Мясо', 'мясо'), o('poultry', 'Птица', 'птица'), o('fish', 'Рыба', 'рыба'), o('veg', 'Без мяса', 'без мяса'), o('mix', 'Всего понемногу', 'всего понемногу')],
  },
  {
    id: 'meat', title: 'Какое мясо?', multi: true, prefix: 'мясо: ',
    options: [o('beef', 'Говядина'), o('pork', 'Свинина'), o('lamb', 'Баранина'), o('mince', 'Фарш')],
    showIf: (a) => (a['base'] ?? []).includes('meat'),
  },
  {
    id: 'novelty', title: 'Новое или проверенное?',
    options: [o('known', 'Проверенное', 'в основном проверенные блюда'), o('some', 'Немного нового', 'пара новых блюд, остальное привычное'), o('new', 'Всё новое', 'всё новое, удиви')],
  },
  // --- «Ещё вопросы» ---
  {
    id: 'format', title: 'Под заморозку или свежим?', extra: true,
    options: [o('freeze', 'Под заморозку', ''), o('fresh', 'На 2–3 дня свежим', 'не под заморозку — на 2–3 дня свежим')],
  },
  { id: 'fat', title: 'Жирность?', extra: true, options: [o('low', 'Поменьше жира', 'поменьше жира'), o('any', 'Без разницы', '')] },
  { id: 'soup', title: 'Суп нужен?', extra: true, options: [o('one', 'Один суп', 'с супом'), o('none', 'Без супа', 'без супа'), o('two', 'Два супа', 'два супа')] },
  {
    id: 'garnish', title: 'Гарнир?', extra: true,
    options: [o('sep', 'Отдельно', 'гарнир отдельно'), o('in', 'В составе блюда', 'гарнир в составе блюда'), o('no', 'Без гарнира', 'без гарнира')],
  },
  {
    id: 'cuisine', title: 'Какая кухня?', multi: true, prefix: 'кухня: ', extra: true,
    options: [o('home', 'Домашняя', 'домашняя'), o('asia', 'Азиатская', 'азиатская'), o('med', 'Средиземноморская', 'средиземноморская'), o('surprise', 'Удиви', 'любая, удиви')],
  },
  { id: 'spicy', title: 'Острота?', extra: true, options: [o('no', 'Без острого', 'без острого'), o('some', 'Чуть-чуть', 'чуть остро'), o('hot', 'Остро', 'остро')] },
  { id: 'budget', title: 'Бюджет?', extra: true, options: [o('eco', 'Эконом', 'эконом'), o('normal', 'Обычно', ''), o('more', 'Можно дороже', 'можно дороже')] },
  {
    id: 'who', title: 'Для кого готовим?', extra: true,
    options: [o('adults', 'Взрослые', ''), o('kids', 'С детьми', 'с детьми — мягкие вкусы'), o('guests', 'Гости', 'для гостей')],
  },
  { id: 'servings', title: 'На сколько человек?', extra: true, options: [o('2', '2', 'на 2 человек'), o('4', '3–4', 'на 3–4 человек'), o('5', '5+', 'на 5+ человек')] },
  {
    id: 'tech', title: 'Какая техника есть?', multi: true, prefix: 'техника: ', extra: true,
    options: [o('stove', 'Плита', 'плита'), o('oven', 'Духовка', 'духовка'), o('multi', 'Мультиварка', 'мультиварка'), o('air', 'Аэрогриль', 'аэрогриль')],
  },
  { id: 'level', title: 'Сложность?', extra: true, options: [o('easy', 'Совсем просто', 'совсем просто'), o('normal', 'Обычно', ''), o('hard', 'Можно повозиться', 'можно повозиться')] },
  { id: 'mood', title: 'Настроение?', extra: true, options: [o('light', 'Лёгкое', 'лёгкое'), o('warm', 'Согревающее', 'согревающее'), o('fest', 'Праздничное', 'праздничное')] },
  { id: 'protein', title: 'Белок?', extra: true, options: [o('more', 'Больше белка', 'больше белка'), o('normal', 'Обычно', '')] },
];

/** Строка выбора из ответов (порядок — порядок шагов; «без разницы» не пишем). */
export function composeChoice(answers: Answers, favorites: string[] = []): string {
  const parts: string[] = [];
  for (const step of WIZARD_STEPS) {
    const ids = answers[step.id] ?? [];
    if (!ids.length || (step.showIf && !step.showIf(answers))) continue;
    if (step.id === 'count') {
      parts.push(`${ids[0]} блюд`);
      continue;
    }
    // В порядке карточек, а не тапов («обеды, ужины»).
    const phrases = step.options.filter((x) => ids.includes(x.id)).map((x) => x.phrase).filter(Boolean);
    if (!phrases.length) continue;
    if (step.multi) parts.push((step.prefix ?? '') + phrases.join(', '));
    else parts.push(phrases[0]);
    if (step.id === 'novelty' && ids[0] === 'known' && favorites.length) {
      parts.push(`можно повторить из избранного: ${favorites.slice(0, 3).join(', ')}`);
    }
  }
  const s = parts.join(' · ');
  return s ? s[0].toUpperCase() + s.slice(1) : '';
}

/** Итоговое сообщение: выбор + уточнение текстом (оно важнее выбора). */
export function composeRequest(choice: string, text: string): string {
  const t = text.trim();
  if (!choice) return t;
  return t ? `${choice}\nУточнение (важнее выбора выше): ${t}` : choice;
}

@Injectable({ providedIn: 'root' })
export class PlanWizard {
  private readonly api = inject(EasyWeekApi);

  readonly answers = signal<Answers>({});
  readonly withExtra = signal(false); // открыты «Ещё вопросы»
  readonly index = signal(0); // текущий шаг среди видимых; = длина → итог
  readonly favorites = signal<string[]>([]);
  private favLoaded = false;

  /** Видимые шаги (основные + «ещё», если открыты; ветвление по showIf). */
  readonly steps = computed(() =>
    WIZARD_STEPS.filter(
      (s) => (!s.extra || this.withExtra()) && (!s.showIf || s.showIf(this.answers())),
    ),
  );
  readonly current = computed<WizStep | null>(() => this.steps()[this.index()] ?? null);
  readonly finished = computed(() => this.index() >= this.steps().length);
  readonly choice = computed(() => composeChoice(this.answers(), this.favorites()));
  /** Число блюд, если выбрано (иначе — прежнее значение чата). */
  readonly count = computed(() => {
    const n = Number(this.answers()['count']?.[0]);
    return Number.isFinite(n) && n > 0 ? n : null;
  });

  loadFavorites(): void {
    if (this.favLoaded) return;
    this.favLoaded = true;
    this.api.listRecipes().subscribe({
      next: (list) => {
        const seen = new Set<string>();
        const names: string[] = [];
        for (const r of list) {
          if (r.favorite && !seen.has(r.key)) {
            seen.add(r.key);
            names.push(r.name);
          }
        }
        this.favorites.set(names);
      },
      error: () => (this.favLoaded = false),
    });
  }

  isOn(step: WizStep, id: string): boolean {
    return (this.answers()[step.id] ?? []).includes(id);
  }

  /** Тап по карточке: одиночный выбор — отметить и дальше; мультивыбор — переключить. */
  pick(step: WizStep, id: string): void {
    const cur = this.answers()[step.id] ?? [];
    if (step.multi) {
      const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      this.answers.update((a) => ({ ...a, [step.id]: next }));
      return;
    }
    this.answers.update((a) => ({ ...a, [step.id]: [id] }));
    setTimeout(() => this.next(), 160); // видно, что выбрано, и дальше
  }

  next(): void {
    this.index.update((i) => Math.min(i + 1, this.steps().length));
  }

  back(): void {
    this.index.update((i) => Math.max(0, i - 1));
  }

  skip(): void {
    const step = this.current();
    if (step) this.answers.update((a) => ({ ...a, [step.id]: [] }));
    this.next();
  }

  /** «Ещё вопросы» с итога: открываем дополнительные шаги с первого из них. */
  more(): void {
    const before = this.steps().length;
    this.withExtra.set(true);
    this.index.set(before);
  }

  edit(): void {
    this.index.set(0);
  }

  reset(): void {
    this.answers.set({});
    this.withExtra.set(false);
    this.index.set(0);
  }
}
