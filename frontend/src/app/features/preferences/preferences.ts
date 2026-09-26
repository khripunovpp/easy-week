import { ChangeDetectionStrategy, Component, DestroyRef, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import {
  EasyWeekApi,
  FoodPreferences,
  FoodPrefsPatch,
  MacroLevel,
  Macros,
} from '../../services/api';
import { CookingLoader } from '../../shared/cooking-loader';
import { ChipEditor, PREF_ITEM_MAX, PREF_LIST_MAX } from './chip-editor';

type ListKey = 'allergies' | 'likes' | 'dislikes';
type SaveState = 'idle' | 'saving' | 'saved';

/** Пауза перед сохранением: серия тапов по чипам/БЖУ уходит одним PUT. */
const SAVE_DEBOUNCE_MS = 600;

const EMPTY: FoodPreferences = {
  allergies: [],
  likes: [],
  dislikes: [],
  suggestedAllergies: [],
  macros: { protein: 'normal', fat: 'normal', carbs: 'normal' },
  dietNote: '',
};

const same = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();
const without = (list: string[], v: string) => list.filter((x) => !same(x, v));

/**
 * Экран «Предпочтения» (/preferences): аллергии, нравится / не нравится, акцент БЖУ, заметка.
 * Сохраняется сам (debounce): оптимистично правим локально, шлём ЧАСТИЧНЫЙ PUT только
 * изменённых полей (чтобы не затереть подсказки, которые фоном дописал чат); при ошибке —
 * откат к последнему сохранённому и inline-ошибка.
 */
@Component({
  selector: 'ew-preferences',
  imports: [RouterLink, CookingLoader, ChipEditor],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './preferences.html',
  styleUrl: './preferences.scss',
})
export class PreferencesPage {
  private readonly api = inject(EasyWeekApi);

  readonly prefs = signal<FoodPreferences>(EMPTY);
  readonly loading = signal(true);
  readonly loadFailed = signal(false);
  readonly saveState = signal<SaveState>('idle');
  readonly error = signal<string | null>(null);

  /** Последнее, что подтвердил сервер, — точка отката при ошибке сохранения. */
  private saved: FoodPreferences = EMPTY;
  /** Поля, изменённые с последней отправки (уходят в PUT). */
  private dirty = new Set<keyof FoodPreferences>();
  private timer: ReturnType<typeof setTimeout> | null = null;

  readonly itemMax = PREF_ITEM_MAX;

  readonly macroRows: { key: keyof Macros; label: string }[] = [
    { key: 'protein', label: 'Белки' },
    { key: 'fat', label: 'Жиры' },
    { key: 'carbs', label: 'Углеводы' },
  ];
  readonly macroOptions: { value: MacroLevel; label: string }[] = [
    { value: 'low', label: 'Меньше' },
    { value: 'normal', label: 'Норма' },
    { value: 'high', label: 'Больше' },
  ];

  constructor() {
    this.load();
    // ушли со страницы с несохранённым — отправляем сразу, не теряем правку
    inject(DestroyRef).onDestroy(() => {
      if (this.timer) {
        clearTimeout(this.timer);
        this.flush();
      }
    });
  }

  load(): void {
    this.loading.set(true);
    this.loadFailed.set(false);
    this.api.getPreferences().subscribe({
      next: (p) => {
        this.saved = { ...EMPTY, ...p, macros: { ...EMPTY.macros, ...p.macros } };
        this.prefs.set(this.saved);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.loadFailed.set(true);
      },
    });
  }

  // ---- Правки (оптимистично + отложенное сохранение) ----

  private patch(next: Partial<FoodPreferences>): void {
    this.prefs.update((cur) => ({ ...cur, ...next }));
    for (const k of Object.keys(next) as (keyof FoodPreferences)[]) this.dirty.add(k);
    this.error.set(null);
    this.saveState.set('saving');
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), SAVE_DEBOUNCE_MS);
  }

  addItem(kind: ListKey, raw: string): void {
    const v = raw.trim().slice(0, PREF_ITEM_MAX);
    const cur = this.prefs();
    if (!v || cur[kind].some((x) => same(x, v)) || cur[kind].length >= PREF_LIST_MAX) return;
    const next: Partial<FoodPreferences> = { [kind]: [...cur[kind], v] };
    // Явный выбор пользователя разводит списки: одно и то же не бывает и «нравится», и «нельзя».
    if (kind === 'allergies') {
      next.likes = without(cur.likes, v);
      next.suggestedAllergies = without(cur.suggestedAllergies, v);
    } else if (kind === 'likes') {
      next.dislikes = without(cur.dislikes, v);
    } else {
      next.likes = without(cur.likes, v);
    }
    this.patch(next);
  }

  removeItem(kind: ListKey, item: string): void {
    this.patch({ [kind]: this.prefs()[kind].filter((x) => x !== item) });
  }

  /** «Добавить в аллергии?» → да. */
  acceptSuggestion(item: string): void {
    this.addItem('allergies', item);
  }

  /** «Добавить в аллергии?» → нет (просто убираем подсказку). */
  dismissSuggestion(item: string): void {
    this.patch({ suggestedAllergies: this.prefs().suggestedAllergies.filter((x) => x !== item) });
  }

  setMacro(key: keyof Macros, level: MacroLevel): void {
    if (this.prefs().macros[key] === level) return;
    this.patch({ macros: { ...this.prefs().macros, [key]: level } });
  }

  setNote(raw: string): void {
    const v = raw.trim().slice(0, 200);
    if (v !== this.prefs().dietNote) this.patch({ dietNote: v });
  }

  // ---- Сохранение ----

  private flush(): void {
    this.timer = null;
    if (!this.dirty.size) return;
    const cur = this.prefs();
    const body: FoodPrefsPatch = {};
    for (const k of this.dirty) (body as Record<string, unknown>)[k] = cur[k];
    this.dirty.clear();
    const sent = cur;
    this.api.setPreferences(body).subscribe({
      next: (p) => {
        this.saved = p;
        // пока летел запрос, могли нажать ещё — локальное новее, его не трогаем
        if (this.prefs() === sent) this.prefs.set(p);
        if (!this.timer) this.saveState.set('saved');
      },
      error: (e: { status?: number }) => {
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
        this.dirty.clear();
        this.prefs.set(this.saved); // откат к последнему подтверждённому
        this.saveState.set('idle');
        this.error.set(
          e?.status === 422
            ? 'Слишком длинно или слишком много пунктов — изменения отменены.'
            : 'Не удалось сохранить — изменения отменены. Проверьте связь и повторите.',
        );
      },
    });
  }
}
