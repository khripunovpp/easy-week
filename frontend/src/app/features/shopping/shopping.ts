import { Component, computed, effect, inject, input, linkedSignal, signal } from '@angular/core';
import { Router } from '@angular/router';
import { EasyWeekApi, PlanSummary, ShoppingGroup, ShoppingListItem } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { ModelSettings } from '../../services/model-settings';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { PlanPicker } from '../../shared/plan-picker';
import { formatGeneratedAt } from '../../shared/format';
import { Vote } from '../../shared/vote';

// Порядок категорий в списке (как на бэке). Незнакомые — в конце.
const CATEGORY_ORDER = [
  'Мясо и птица',
  'Рыба',
  'Овощи',
  'Молочное',
  'Бакалея',
  'Специи',
  'Прочее',
];

@Component({
  selector: 'ew-shopping',
  imports: [CookingLoader, PlanPicker, Vote],
  templateUrl: './shopping.html',
  styleUrl: './shopping.scss',
})
export class Shopping {
  /** Когда собран список покупок на сервере (из плана) — для подписи «собран …». */
  readonly shoppingAt = signal<string | null>(null);
  /** Модель, собравшая текущий список (ключ) — оценка 👍/👎 привязана к ней. */
  readonly shoppingModelKey = signal('');

  /** Подпись даты генерации: «сегодня, 14:05» / «26 сен, 14:05»; пусто — не показываем. */
  genAt(iso: string | null | undefined): string {
    return formatGeneratedAt(iso);
  }

  private readonly api = inject(EasyWeekApi);
  private readonly store = inject(ChatStore);
  private readonly router = inject(Router);
  private readonly modelSettings = inject(ModelSettings);

  // /shopping/:planId — конкретный план; /shopping — выбранный «текущий» (или принятый/первый).
  readonly planId = input<string>('');

  readonly items = signal<ShoppingListItem[]>([]);
  readonly plans = signal<PlanSummary[]>([]);
  readonly selectedId = signal('');
  readonly loading = signal(true);
  readonly empty = signal(false);
  readonly title = signal('');
  readonly currentPlanId = signal('');

  // «↻ Перегенерировать»: пересборка идёт (старый список на экране) / ошибка последней попытки.
  readonly regenerating = signal(false);
  readonly regenError = signal('');
  private readonly opening = signal(false); // «💬 Обсудить»: ждём conversationId плана
  readonly busy = computed(() => this.regenerating() || this.opening() || this.loading());
  // Футер действий — когда список есть.
  readonly hasFooter = computed(() => !this.empty() && this.items().length > 0);

  // Модель нормализации для «↻ Перегенерировать»: стартует с настройки «Список покупок»
  // (общая, на сервере), выпадашка в шапке меняет её только для этой страницы.
  readonly shopModel = linkedSignal<RecipeModel>(() => this.modelSettings.models().shopping);
  readonly defaultShopModel = computed(() => this.modelSettings.models().shopping);
  readonly modelMenuOpen = signal(false);
  readonly allModels = ALL_MODELS;

  private readonly checked = signal<Set<string>>(new Set());
  private activePlanId = '';

  // Живая группировка по категориям. Отмеченные (купленные) уезжают в конец
  // своей категории, невыбранные — сверху; внутри — по алфавиту.
  readonly groups = computed<ShoppingGroup[]>(() => {
    const checked = this.checked();
    const byCat = new Map<string, ShoppingListItem[]>();
    for (const it of this.items()) {
      const cat = it.category || 'Прочее';
      (byCat.get(cat) ?? byCat.set(cat, []).get(cat)!).push(it);
    }
    const order = [
      ...CATEGORY_ORDER.filter((c) => byCat.has(c)),
      ...[...byCat.keys()].filter((c) => !CATEGORY_ORDER.includes(c)),
    ];
    return order.map((category) => ({
      category,
      items: [...byCat.get(category)!].sort((a, b) => {
        const da = checked.has(this.key(a)) ? 1 : 0;
        const db = checked.has(this.key(b)) ? 1 : 0;
        if (da !== db) return da - db; // невыбранные сверху, отмеченные — вниз
        return a.name.localeCompare(b.name, 'ru');
      }),
    }));
  });

  readonly total = computed(() => this.items().length);
  readonly doneCount = computed(() => this.checked().size);
  // Даты текущего плана — в подзаголовок шапки.
  readonly currentWeekLabel = computed(
    () => this.plans().find((p) => p.id === this.selectedId())?.weekLabel ?? '',
  );

  constructor() {
    this.modelSettings.ensureLoaded();
    effect(() => {
      const pid = this.planId();
      this.load(pid);
    });
  }

  private load(inputPlanId: string): void {
    this.loading.set(true);
    this.empty.set(false);
    this.items.set([]);
    this.api.listPlans().subscribe({
      next: (list) => {
        this.plans.set(list);
        if (!list.length) {
          this.loading.set(false);
          this.empty.set(true);
          return;
        }
        const resolve = (curId: string | null) => {
          const inList = (id: string) => list.some((p) => p.id === id);
          const target =
            (inputPlanId && inList(inputPlanId) && inputPlanId) ||
            (curId && inList(curId) && curId) ||
            list.find((p) => p.status === 'accepted')?.id ||
            list[0].id;
          this.selectedId.set(target);
          this.title.set(list.find((p) => p.id === target)?.title ?? '');
          // Прямая ссылка на конкретный план → делаем его текущим (как выбор в селекторе).
          if (inputPlanId && target === inputPlanId) this.api.setCurrentPlan(target).subscribe();
          this.fetch(target);
        };
        // Явный план в URL — только если это актуальная версия (из списка); ссылка на
        // заменённую правкой версию → берём «текущий» с сервера (он уже указывает на новую).
        if (inputPlanId && list.some((p) => p.id === inputPlanId)) resolve(null);
        else
          this.api.getCurrentPlan().subscribe({
            next: (r) => resolve(r.planId),
            error: () => resolve(null),
          });
      },
      error: () => {
        this.loading.set(false);
        this.empty.set(true);
      },
    });
  }

  // Смена плана из селектора: сохраняем выбор на сервере (общий) и грузим его.
  onPickPlan(id: string): void {
    this.selectedId.set(id);
    this.title.set(this.plans().find((p) => p.id === id)?.title ?? '');
    this.api.setCurrentPlan(id).subscribe();
    this.fetch(id);
  }

  private fetch(planId: string): void {
    this.activePlanId = planId;
    this.currentPlanId.set(planId);
    this.checked.set(this.loadChecked(planId));
    this.shoppingAt.set(null);
    this.shoppingModelKey.set('');

    // Мгновенно показываем закэшированный список (в т.ч. офлайн), затем обновляем с сервера.
    const cached = this.loadItems(planId);
    this.items.set(cached);
    if (cached.length) this.loading.set(false);

    this.api.shoppingList(planId).subscribe({
      next: (groups) => {
        const items = groups.flatMap((g) => g.items);
        this.items.set(items);
        this.saveItems(planId, items);
        this.loading.set(false);
        this.empty.set(items.length === 0);
        // Время сборки хранится в плане — подтягиваем для подписи «собран …».
        this.api.getPlan(planId).subscribe({
          next: (p) => {
            if (this.activePlanId !== planId) return;
            this.shoppingAt.set(p.shoppingGeneratedAt ?? null);
            this.shoppingModelKey.set(p.shoppingModel ?? '');
          },
        });
      },
      error: () => {
        // Офлайн/ошибка — остаёмся на кэше, если он есть.
        this.loading.set(false);
        this.empty.set(this.items().length === 0);
      },
    });
  }

  // «↻ Перегенерировать»: нормализация списка мимо кэша с учётом обсуждения покупок в чате.
  // Отметки «куплено» переносим на новые позиции, если название совпало (единица могла
  // смениться); остальные отметки отпадают. При ошибке старый список остаётся.
  regenerate(): void {
    const pid = this.currentPlanId();
    if (!pid || this.busy()) return;
    this.regenerating.set(true);
    this.regenError.set('');
    this.modelMenuOpen.set(false);
    this.api.regenerateShopping(pid, this.shopModel()).subscribe({
      next: (groups) => {
        const items = groups.flatMap((g) => g.items);
        const checkedNames = new Set(
          this.items()
            .filter((it) => this.checked().has(this.key(it)))
            .map((it) => it.name.toLowerCase()),
        );
        this.items.set(items);
        this.saveItems(pid, items);
        this.checked.set(
          new Set(
            items.filter((it) => checkedNames.has(it.name.toLowerCase())).map((it) => this.key(it)),
          ),
        );
        this.saveChecked();
        this.shoppingAt.set(new Date().toISOString()); // только что пересобран
        this.shoppingModelKey.set(this.shopModel());
        this.regenerating.set(false);
      },
      error: (err) => {
        this.regenError.set(err?.error?.detail ?? 'Не удалось пересобрать список покупок.');
        this.regenerating.set(false);
      },
    });
  }

  modelLabel(key: string): string {
    return MODEL_LABELS[key as RecipeModel] ?? key;
  }

  toggleModelMenu(): void {
    this.modelMenuOpen.update((v) => !v);
  }

  // Выбор модели нормализации — только для этой страницы (настройки не меняем).
  // Сам список не пересобираем: новая модель сработает по «↻ Перегенерировать».
  pickModel(m: RecipeModel): void {
    this.shopModel.set(m);
    this.modelMenuOpen.set(false);
  }

  // «💬 Обсудить в чате»: беседа плана + бейдж «Обсуждение: список покупок».
  discuss(): void {
    const pid = this.currentPlanId();
    if (!pid || this.busy()) return;
    this.opening.set(true);
    this.api.getPlan(pid).subscribe({
      next: (p) => {
        this.opening.set(false);
        if (!p.conversationId) return;
        this.store.startDiscuss({
          conversationId: p.conversationId,
          target: 'shopping',
          planId: p.id,
          name: 'список покупок',
        });
        this.router.navigate(['/chat']);
      },
      error: () => {
        this.opening.set(false);
        this.regenError.set('Не удалось открыть чат плана.');
      },
    });
  }

  key(item: { name: string; unit: string }): string {
    return `${item.name.toLowerCase()}__${item.unit}`;
  }

  isChecked(item: { name: string; unit: string }): boolean {
    return this.checked().has(this.key(item));
  }

  toggle(item: { name: string; unit: string }): void {
    const k = this.key(item);
    this.checked.update((set) => {
      const next = new Set(set);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });
    this.saveChecked();
  }

  // Состояние группы: все отмечены / часть / ничего — для чекбокса группы.
  groupState(group: ShoppingGroup): 'all' | 'some' | 'none' {
    const items = group.items;
    if (!items.length) return 'none';
    let on = 0;
    for (const it of items) if (this.checked().has(this.key(it))) on++;
    return on === 0 ? 'none' : on === items.length ? 'all' : 'some';
  }

  // Клик по группе: если всё отмечено — снять всё, иначе отметить всё.
  toggleGroup(group: ShoppingGroup): void {
    const turnOff = this.groupState(group) === 'all';
    this.checked.update((set) => {
      const next = new Set(set);
      for (const it of group.items) {
        const k = this.key(it);
        if (turnOff) next.delete(k);
        else next.add(k);
      }
      return next;
    });
    this.saveChecked();
  }

  fmtQty(item: { qty: number; unit: string }): string {
    return `${item.qty} ${item.unit}`;
  }

  private storageKey(planId: string): string {
    return `ew-shopping-${planId}`;
  }

  // Кэш самих позиций списка (для мгновенного показа и офлайна).
  private itemsKey(planId: string): string {
    return `ew-shopping-items-${planId}`;
  }
  private loadItems(planId: string): ShoppingListItem[] {
    try {
      const raw = localStorage.getItem(this.itemsKey(planId));
      return raw ? (JSON.parse(raw) as ShoppingListItem[]) : [];
    } catch {
      return [];
    }
  }
  private saveItems(planId: string, items: ShoppingListItem[]): void {
    try {
      localStorage.setItem(this.itemsKey(planId), JSON.stringify(items));
    } catch {
      /* localStorage может быть недоступен — не критично */
    }
  }

  private loadChecked(planId: string): Set<string> {
    try {
      const raw = localStorage.getItem(this.storageKey(planId));
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  }

  private saveChecked(): void {
    if (!this.activePlanId) return;
    localStorage.setItem(
      this.storageKey(this.activePlanId),
      JSON.stringify([...this.checked()]),
    );
  }
}
