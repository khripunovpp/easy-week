import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CatalogModel, EasyWeekApi, LimitsStatus, ModelTask, UsageDay, UsageProvider, UsageStats } from '../../services/api';
import { ModelSettings } from '../../services/model-settings';
import { MODEL_LABELS, RecipeModel } from '../../services/preferences';
import { ModelName } from '../../shared/model-name';
import { dayGroupLabel, formatUsd, plural } from '../../shared/format';

// Экран «Модели по умолчанию» (/settings/models, под-экран профиля): модель для каждой задачи.
// Хранится на сервере (общая для всех устройств семьи); страницы стартуют с неё, но могут
// выбрать другую локально.
@Component({
  selector: 'ew-settings-models',
  imports: [RouterLink, ModelName],
  templateUrl: './settings-models.html',
  styleUrl: './settings-models.scss',
})
export class SettingsModelsPage {
  readonly models = inject(ModelSettings);
  private readonly api = inject(EasyWeekApi);

  // Дневные лимиты: озвучка; Claude — только если лимит включён (ANTHROPIC_DAILY_*, по умолчанию 0)
  // и Claude выбран хоть для одной задачи.
  readonly limits = signal<LimitsStatus | null>(null);
  readonly usesClaude = computed(() => Object.values(this.models.models()).includes('anthropic'));
  readonly claudeLimited = computed(() => {
    const a = this.limits()?.anthropic;
    return !!a && (a.plans.limit > 0 || a.recipes.limit > 0);
  });
  // Статистика запросов к моделям за 7 дней (из AI-логов); дни без запросов не показываем.
  readonly usage = signal<UsageStats | null>(null);
  readonly usageDays = computed(() =>
    (this.usage()?.days ?? []).filter((d) => d.calls > 0 || d.errors > 0),
  );
  // Какая выпадашка открыта (одна за раз).
  readonly openTask = signal<ModelTask | null>(null);

  // Строки: задача → подпись и что в неё входит. Какие модели предлагать в каждой строке —
  // карта с сервера (models.modelsFor(task)): дешёвые модели на рецептах/готовке не показываем.
  readonly taskRows: { task: ModelTask; label: string; hint: string }[] = [
    { task: 'chat', label: 'Чат и план', hint: 'план недели, правки, обсуждение' },
    { task: 'recipe', label: 'Рецепты', hint: 'рецепт блюда, догенерация для PDF' },
    { task: 'shopping', label: 'Список покупок', hint: 'сведение и чистка списка' },
    { task: 'cooking', label: 'План готовки', hint: 'порядок готовки всех блюд' },
    { task: 'prefs', label: 'Предпочтения из чата', hint: 'фоновое извлечение вкусов из сообщений' },
    { task: 'summary', label: 'Сводка чата', hint: 'краткая память беседы для ответов в чате' },
    { task: 'fix', label: 'Правка рецепта', hint: '«Исправить» в рецепте: убрать или заменить продукт' },
  ];

  constructor() {
    this.models.refresh(); // свежие значения (могли поменять с другого устройства)
    this.api.limits().subscribe({ next: (l) => this.limits.set(l) });
    this.api.usage(7).subscribe({ next: (u) => this.usage.set(u) });
  }

  // --- Статистика запросов (GUIDEBOOK «Экраны настроек» → «Статистика запросов») ---

  dayLabel(d: UsageDay): string {
    return dayGroupLabel(`${d.date}T12:00:00`); // локальный полдень — без сдвига дня по поясу
  }

  callsWord(n: number): string {
    return plural(n, ['запрос', 'запроса', 'запросов']);
  }

  calls(n: number): string {
    return `${n} ${this.callsWord(n)}`;
  }

  errors(n: number): string {
    return `${n} ${plural(n, ['сбой', 'сбоя', 'сбоев'])}`;
  }

  usd(v: number): string {
    return formatUsd(v);
  }

  /** Стоимость провайдера за день — только заметная (от цента), иначе пусто. */
  providerCost(p: UsageProvider): string {
    return p.costUsd >= 0.01 ? formatUsd(p.costUsd) : '';
  }

  modelLabel(key: RecipeModel): string {
    return MODEL_LABELS[key] ?? key;
  }

  toggleTask(task: ModelTask): void {
    this.openTask.update((cur) => (cur === task ? null : task));
  }

  /** Вторая строка пункта: id модели (без «@cf/…/») и пометка. */
  optSub(m: CatalogModel): string {
    const id = m.id.startsWith('@cf/') ? (m.id.split('/').pop() ?? m.id) : m.id;
    return m.note ? `${id} · ${m.note}` : id;
  }

  /** Выбрана ли конкретная модель provider:id для задачи (модель по умолчанию — через fullRef). */
  isPicked(task: ModelTask, provider: string, id: string): boolean {
    return this.models.fullRef(this.models.refs()[task]) === `${provider}:${id}`;
  }

  pickModel(task: ModelTask, provider: string, id: string): void {
    this.openTask.set(null);
    const ref = this.models.makeRef(provider, id);
    if (this.models.refs()[task] !== ref) this.models.set(task, ref);
  }
}
