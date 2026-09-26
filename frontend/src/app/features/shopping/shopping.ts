import { NgTemplateOutlet } from '@angular/common';
import { Component, computed, effect, inject, input, linkedSignal, signal } from '@angular/core';
import { Router } from '@angular/router';
import { DishShopping, EasyWeekApi, PlanSummary, ShoppingGroup, ShoppingListItem } from '../../services/api';
import { ChatStore } from '../../services/chat-store';
import { ModelSettings } from '../../services/model-settings';
import { ALL_MODELS, MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { CookingLoader } from '../../shared/cooking-loader';
import { PlanPicker } from '../../shared/plan-picker';
import { formatGeneratedAt } from '../../shared/format';
import { Vote } from '../../shared/vote';
import { productKey, sameProduct } from '../../shared/product-key';
import { ModelName } from '../../shared/model-name';

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
  imports: [NgTemplateOutlet, CookingLoader, PlanPicker, Vote, ModelName],
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

  // Отметки «куплено» — ОДНИ на оба режима: множество ключей продуктов (productKey).
  // Отметил лук в «Общем» — он отмечен и во всех блюдах в «По рецептам», и наоборот.
  private readonly checked = signal<Set<string>>(new Set());
  private activePlanId = '';

  // ---- Режим группировки: «Общий» (по категориям) / «По рецептам» (блюдо → категории) ----
  readonly mode = signal<'all' | 'dish'>(this.loadMode());
  /** Покупки по блюдам (грузим лениво при первом переключении на «По рецептам»). */
  readonly byDish = signal<DishShopping[]>([]);
  readonly byDishLoading = signal(false);
  readonly byDishError = signal(false);
  private byDishPlanId = '';

  setMode(m: 'all' | 'dish'): void {
    this.mode.set(m);
    try {
      localStorage.setItem('ew-shopping-mode', m);
    } catch {
      /* приватный режим — просто не запоминаем */
    }
    if (m === 'dish') this.ensureByDish();
  }

  private loadMode(): 'all' | 'dish' {
    try {
      return localStorage.getItem('ew-shopping-mode') === 'dish' ? 'dish' : 'all';
    } catch {
      return 'all';
    }
  }

  /** Догрузить покупки по блюдам для текущего плана (один раз на план). */
  private ensureByDish(force = false): void {
    const pid = this.currentPlanId();
    if (!pid || (!force && this.byDishPlanId === pid && this.byDish().length)) return;
    this.byDishPlanId = pid;
    this.byDishLoading.set(true);
    this.byDishError.set(false);
    this.api.shoppingByDish(pid).subscribe({
      next: (list) => {
        if (this.byDishPlanId !== pid) return;
        this.byDish.set(list);
        this.byDishLoading.set(false);
      },
      error: () => {
        this.byDishLoading.set(false);
        this.byDishError.set(true);
      },
    });
  }

  /** Секции режима «По рецептам»: блюдо → группы по категориям (отметки — свои для блюда). */
  readonly dishSections = computed(() =>
    this.byDish().map((d) => ({
      ...d,
      prefix: `${d.dishId}::`,
      groups: this.groupByCategory(d.items, `${d.dishId}::`),
    })),
  );

  // Живая группировка по категориям (режим «Общий»).
  readonly groups = computed<ShoppingGroup[]>(() => this.groupByCategory(this.items(), ''));

  // Отмеченные (купленные) уезжают в конец своей категории, невыбранные — сверху;
  // внутри — по алфавиту. prefix непустой — это список блюда («По рецептам»).
  private groupByCategory(list: ShoppingListItem[], prefix: string): ShoppingGroup[] {
    const byCat = new Map<string, ShoppingListItem[]>();
    for (const it of list) {
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
        const da = this.isChecked(a, prefix) ? 1 : 0;
        const db = this.isChecked(b, prefix) ? 1 : 0;
        if (da !== db) return da - db; // невыбранные сверху, отмеченные — вниз
        return a.name.localeCompare(b.name, 'ru');
      }),
    }));
  }

  // Счётчик «отмечено / всего» — по строкам текущего режима (отметки общие).
  private readonly visibleItems = computed(() =>
    this.mode() === 'dish'
      ? this.byDish().flatMap((d) => d.items.map((it) => ({ it, dish: true })))
      : this.items().map((it) => ({ it, dish: false })),
  );
  readonly total = computed(() => this.visibleItems().length);
  readonly doneCount = computed(() => {
    this.checked(); // зависимость: пересчёт при отметке
    return this.visibleItems().filter((v) => this.isChecked(v.it, v.dish ? 'dish' : '')).length;
  });
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
    this.byDish.set([]);
    this.byDishPlanId = '';
    if (this.mode() === 'dish') queueMicrotask(() => this.ensureByDish());

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
        // Отметки привязаны к продуктам (не к строкам) — переносятся сами.
        const items = groups.flatMap((g) => g.items);
        this.items.set(items);
        this.saveItems(pid, items);
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

  /** Ключ продукта строки (единица не важна: купил лук — купил). */
  key(item: { name: string }): string {
    return productKey(item.name);
  }

  /** Ключи общего списка, соответствующие строке блюда: точное совпадение продукта, иначе —
   *  «похожие» (лук ↔ лук репчатый). Нет в общем списке — собственный ключ строки. */
  private targetKeys(item: { name: string }): string[] {
    const k = this.key(item);
    const general = this.items().map((it) => this.key(it));
    if (general.includes(k)) return [k];
    const similar = general.filter((g) => sameProduct(g, k));
    return similar.length ? similar : [k];
  }

  /** prefix пустой — строка общего списка (точный ключ); непустой — строка блюда
   *  (отмечена, если отмечен соответствующий продукт общего списка). */
  isChecked(item: { name: string; unit?: string }, prefix = ''): boolean {
    const set = this.checked();
    const k = this.key(item);
    if (set.has(k)) return true;
    if (!prefix) return false;
    return this.targetKeys(item).some((t) => set.has(t));
  }

  toggle(item: { name: string; unit?: string }, prefix = ''): void {
    this.setChecked([item], !this.isChecked(item, prefix), prefix);
  }

  /** Отметить/снять строки: общий список — по точному ключу; блюдо — вместе с продуктом
   *  общего списка, чтобы отметка была видна в обоих режимах. */
  private setChecked(items: { name: string }[], on: boolean, prefix: string): void {
    this.checked.update((set) => {
      const next = new Set(set);
      for (const it of items) {
        const keys = prefix ? [this.key(it), ...this.targetKeys(it)] : [this.key(it)];
        for (const k of keys) {
          if (!k) continue; // имя без букв/цифр — ключа нет, не храним
          on ? next.add(k) : next.delete(k);
        }
      }
      return next;
    });
    this.saveChecked();
  }

  // Состояние группы: все отмечены / часть / ничего — для чекбокса группы.
  groupState(group: ShoppingGroup, prefix = ''): 'all' | 'some' | 'none' {
    const items = group.items;
    if (!items.length) return 'none';
    let on = 0;
    for (const it of items) if (this.isChecked(it, prefix)) on++;
    return on === 0 ? 'none' : on === items.length ? 'all' : 'some';
  }

  // Клик по группе: если всё отмечено — снять всё, иначе отметить всё.
  toggleGroup(group: ShoppingGroup, prefix = ''): void {
    const turnOff = this.groupState(group, prefix) === 'all';
    this.setChecked(group.items, !turnOff, prefix);
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
      const keys = raw ? (JSON.parse(raw) as string[]) : [];
      // Старый формат: «name__unit» и «dishId::name__unit» → ключ продукта.
      return new Set(
        keys
          .map((k) => (k.includes('__') ? productKey(k.split('::').pop()!.split('__')[0]) : k))
          .filter(Boolean),
      );
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
