import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, shareReplay } from 'rxjs';
import {
  ChatMessage,
  DiscussTarget,
  Dish,
  Ingredient,
  PlanStatus,
  WeekPlan,
} from '../models/plan.model';

export interface DishVariant {
  model: string;
  provider: string;
  ingredients: Ingredient[];
  steps: string[];
  tips: string[];
  note: string;
  generatedAt?: string; // когда сгенерирован вариант (ISO)
}

// Единый план готовки на весь недельный план (по всем блюдам).
export interface CookingStep {
  order: number;
  phase: string;
  text: string;
  activeMin: number;
  passiveMin: number;
  dishes: string[];
}
export interface CookingPlan {
  activeModel: string;
  variantModels: string[];
  provider: string;
  steps: CookingStep[];
  note: string;
  generatedAt?: string; // когда сгенерирован активный вариант (ISO)
}
export interface CookingPlanVariant {
  model: string;
  provider: string;
  steps: CookingStep[];
  note: string;
  generatedAt?: string; // когда сгенерирован вариант (ISO)
}
import { Preferences, RecipeModel } from './preferences';
import { AuthService } from './auth';

// Относительный путь: в проде nginx проксирует /api → бэкенд;
// в деве — dev-прокси Angular (proxy.conf.json) на localhost:8000.
const API_BASE = '/api';
// Ревизия URL озвучки: старые ответы /api/tts браузер держал 30 дней (max-age) и со случайными
// голосами — меняем URL один раз. Дальше бэк отдаёт no-cache + ETag, смену голоса видно сразу.
const TTS_REV = '2';

export interface ChatResponse {
  conversationId: string;
  reply: string;
  plan: WeekPlan | null;
  messageId?: string;
  model?: string;
}

// Ответ режима «Обсуждение» (POST /chat/discuss). op — что применено по явной просьбе:
// edit — рецепт обновлён (dish), regenerate — пересобраны готовка/покупки, replace —
// модель предлагает заменить блюдо (suggestReplace + replaceQuery), none — просто ответ.
export interface DiscussResponse {
  conversationId: string;
  reply: string;
  messageId: string;
  model: string;
  target: DiscussTarget;
  planId: string;
  dishId: string | null;
  op: 'none' | 'edit' | 'replace' | 'regenerate';
  dish: Dish | null;
  cooking: CookingPlan | null;
  shopping: ShoppingGroup[] | null;
  suggestReplace: boolean;
  replaceQuery: string;
  applyError: string;
}

// Действие над рецептом/планом готовки: open — активный вариант; select — сделать модель
// активной; regenerate — «↻ Перегенерировать» (всегда новый вариант с учётом обсуждения).
export type VariantAction = 'open' | 'select' | 'regenerate';

export interface MessageSearchHit {
  id: string;
  conversationId: string;
  role: 'user' | 'assistant';
  text: string;
  planTitle: string | null;
  planEmoji: string | null;
}

export type RatingTarget = 'recipe' | 'plan' | 'cooking' | 'shopping' | 'message';
export interface RatingBody {
  targetType: RatingTarget;
  targetId: string;
  model: string;
  vote: 1 | -1;
  planId?: string;
  dishId?: string;
  conversationId?: string;
}

// Состояние голоса: vote 1|-1|0; locksAt — до какого момента можно менять (ISO UTC), '' — голоса нет.
export interface RatingState {
  vote: number;
  locksAt?: string;
}

// Причина 👎 из каталога бэка (GET /api/ratings/reasons); key 'other' — «Другое» + текст.
export interface RatingReason {
  key: string;
  label: string;
}
export type RatingReasonCatalog = Partial<Record<RatingTarget, RatingReason[]>>;

export interface ChatStreamMeta {
  conversationId: string;
  planId: string;
  title: string;
  weekLabel: string;
  reply: string;
  provider?: string;
}

// Финал потока: id плана/сообщения, модель и остатки, которые модель выделила из запроса.
export interface ChatStreamDone {
  planId: string;
  dishesCount: number;
  messageId?: string;
  model?: string;
  leftovers?: string[];
}

export interface ChatStreamHandlers {
  onMeta: (meta: ChatStreamMeta) => void;
  onDish: (dish: Dish) => void;
  onDone: (info: ChatStreamDone) => void;
  onError: (message: string) => void;
}

export interface ShoppingListItem {
  name: string;
  qty: number;
  unit: string;
  category: string;
}

// Покупки одного блюда (режим «По рецептам»).
export interface DishShopping {
  dishId: string;
  name: string;
  emoji: string;
  items: ShoppingListItem[];
}

// Цена модели (USD за 1M токенов; Cloudflare — ещё и за 1000 нейронов).
export interface ModelPrice {
  input: number;
  cachedInput: number;
  cacheWrite: number;
  output: number;
  per1KNeurons?: number | null;
}

export interface FoodPrefs {
  dislikes: string[];
  likes: string[];
}

/** Акцент БЖУ: меньше / норма / больше. */
export type MacroLevel = 'low' | 'normal' | 'high';
export interface Macros {
  protein: MacroLevel;
  fat: MacroLevel;
  carbs: MacroLevel;
}

/** Полные предпочтения (экран /preferences). Аллергии — жёсткое ограничение, правятся только
 *  вручную; suggestedAllergies — подозрения из чата («Добавить в аллергии?»);
 *  suggestedDislikes/suggestedLikes — «похоже на вкус» из чата (не наверняка): в «не люблю» /
 *  «люблю» их переносит только пользователь. */
export interface FoodPreferences extends FoodPrefs {
  allergies: string[];
  suggestedAllergies: string[];
  suggestedDislikes: string[];
  suggestedLikes: string[];
  macros: Macros;
  dietNote: string;
}

/** PUT /preferences — частичный: сервер меняет только переданные поля. */
export type FoodPrefsPatch = Partial<Omit<FoodPreferences, 'macros'>> & { macros?: Partial<Macros> };

export interface DailyLimit {
  used: number;
  limit: number;
  remaining: number;
}
export interface LimitsStatus {
  anthropic: { plans: DailyLimit; recipes: DailyLimit };
  tts?: DailyLimit; // озвучка шагов: новых генераций в сутки (limit 0 — без лимита)
}

// Доступна ли новая генерация озвучки (свой дневной лимит / лимит бесплатных моделей OpenRouter).
export interface TtsStatus {
  available: boolean;
  detail: string;
  resetAt: string | null;
}

// Общие настройки (сервер, одни на все устройства): модели по умолчанию по задачам.
// chat — план/правки/обсуждение, recipe — рецепт блюда, shopping — нормализация покупок,
// cooking — план готовки, prefs — фоновое извлечение предпочтений из чата, summary — фоновая
// сводка беседы. initialized=false — ещё ни разу не сохраняли (встроенные дефолты).
export type ModelTask = 'chat' | 'recipe' | 'shopping' | 'cooking' | 'prefs' | 'summary';
// Значение — ссылка на модель: «провайдер» (модель провайдера по умолчанию) или «провайдер:id»
// (конкретная модель из каталога; id OpenRouter сам содержит «:» — режем по первому).
export type ModelRef = string;
export type ModelDefaults = Record<ModelTask, ModelRef>;
// Конкретная модель провайдера (каталог с бэка, services/model_catalog).
export interface CatalogModel {
  id: string;
  label: string;
  note?: string;
}
// Карта «задача → модели, которые можно выбрать» (бэк: services/settings.TASK_MODELS).
export type TaskModels = Record<ModelTask, RecipeModel[]>;
export interface AppSettings {
  models: ModelDefaults;
  initialized: boolean;
  modelNames?: Record<string, string>; // ключ → конкретная модель (для подписей в выпадашках)
  taskModels?: Partial<Record<ModelTask, string[]>>;
  catalog?: Record<string, CatalogModel[]>; // провайдер → модели (модель по умолчанию — первая)
}

export interface PlanSummary {
  id: string;
  title: string;
  weekLabel: string;
  status: PlanStatus;
  dishesCount: number;
  totalCookMin: number;
  emoji: string;
  dishNames: string[];
  createdAt?: string | null; // когда создан план (ISO)
}

// Рецепт из принятого плана (режим «Рецепты» на странице планов). key — нормализованное
// название: ключ избранного (общего для семьи, переживает правки плана) и дедупликации.
export interface RecipeItem {
  key: string;
  planId: string;
  planTitle: string;
  weekLabel: string;
  planDecidedAt?: string | null;
  dishId: string;
  name: string;
  emoji: string;
  tags: string[];
  prepMin: number;
  cookMin: number;
  servings: number;
  hasRecipe: boolean;
  favorite: boolean;
}

export interface ShoppingGroup {
  category: string;
  items: { name: string; qty: number; unit: string; category: string }[];
}

@Injectable({ providedIn: 'root' })
export class EasyWeekApi {
  private readonly http = inject(HttpClient);
  private readonly prefs = inject(Preferences);
  private readonly auth = inject(AuthService);

  chat(
    message: string,
    conversationId: string | null,
    dishesCount = 5,
    recipeModel: RecipeModel = 'deepseek',
  ): Observable<ChatResponse> {
    return this.http.post<ChatResponse>(`${API_BASE}/chat`, {
      message,
      conversationId,
      dishesCount,
      gender: this.prefs.gender(),
      recipeModel,
    });
  }

  // Правка текущего плана диалога. По тексту — tool calling; по кнопкам карточки — минуя его:
  // replaceDishId (заменить блюдо), removeDishId (удалить, без модели), addDish (добавить блюдо).
  editPlan(
    conversationId: string,
    message: string,
    recipeModel: RecipeModel,
    opts: { replaceDishId?: string; removeDishId?: string; addDish?: boolean } = {},
  ): Observable<ChatResponse> {
    return this.http.post<ChatResponse>(`${API_BASE}/chat/edit`, {
      message,
      conversationId,
      gender: this.prefs.gender(),
      recipeModel,
      ...opts,
    });
  }

  // Потоковый чат (SSE): meta → dish (по одному) → done.
  async chatStream(
    message: string,
    conversationId: string | null,
    dishesCount: number,
    recipeModel: RecipeModel,
    handlers: ChatStreamHandlers,
  ): Promise<void> {
    await this.openSse(
      `${API_BASE}/chat/stream`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          conversationId,
          dishesCount,
          gender: this.prefs.gender(),
          recipeModel,
        }),
      },
      handlers.onError,
      (event, payload) => {
        if (event === 'meta') handlers.onMeta(payload as ChatStreamMeta);
        else if (event === 'dish') handlers.onDish(payload as Dish);
        else if (event === 'done')
          handlers.onDone(payload as ChatStreamDone);
        else if (event === 'error')
          handlers.onError((payload as { message?: string }).message ?? 'Ошибка генерации');
      },
    );
  }

  // Общий приём SSE: читает поток, режет по событиям, дёргает onEvent(event, payload).
  private async openSse(
    url: string,
    init: RequestInit,
    onError: (message: string) => void,
    onEvent: (event: string, payload: unknown) => void,
  ): Promise<void> {
    let resp: Response;
    try {
      // Голый fetch мимо HttpClient: интерсептор сюда не попадает. same-origin — кука сессии
      // уходит сама (явно, чтобы не зависеть от дефолта).
      resp = await fetch(url, { credentials: 'same-origin', ...init });
    } catch {
      onError('Нет связи с сервером');
      return;
    }
    if (resp.status === 401) {
      // Сессия протухла — как в интерсепторе: на экран входа.
      onError('Нужно войти заново');
      this.auth.handleUnauthorized();
      return;
    }
    if (!resp.ok || !resp.body) {
      onError('Сервер недоступен');
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sep: number;
        while ((sep = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const parsed = this.parseSse(chunk);
          if (parsed) onEvent(parsed.event, parsed.payload);
        }
      }
    } catch {
      onError('Поток прервался');
    }
  }

  private parseSse(raw: string): { event: string; payload: unknown } | null {
    let event = 'message';
    let data = '';
    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data += line.slice(5).trim();
    }
    if (!data) return null;
    try {
      return { event, payload: JSON.parse(data) };
    } catch {
      return null;
    }
  }

  // Цены моделей для учёта затрат (USD за 1M токенов) — общие, на сервере.
  getPrices(): Observable<{ prices: Record<string, ModelPrice> }> {
    return this.http.get<{ prices: Record<string, ModelPrice> }>(`${API_BASE}/settings/prices`);
  }
  putPrices(prices: Record<string, ModelPrice>): Observable<{ prices: Record<string, ModelPrice> }> {
    return this.http.put<{ prices: Record<string, ModelPrice> }>(`${API_BASE}/settings/prices`, { prices });
  }

  limits(): Observable<LimitsStatus> {
    return this.http.get<LimitsStatus>(`${API_BASE}/limits`);
  }

  /** URL озвучки шага — для <audio src> (играть прямо из тапа, см. shared/tts-player). */
  ttsUrl(text: string): string {
    return `${API_BASE}/tts?text=${encodeURIComponent(text)}&r=${TTS_REV}`;
  }
  /** Почему озвучка недоступна (<audio> текст ошибки не видит) — лимиты, см. бэк routers/tts. */
  ttsStatus(): Observable<TtsStatus> {
    return this.http.get<TtsStatus>(`${API_BASE}/tts/status`);
  }
  /** Аудио шага блобом — фоновый прогрев остальных шагов рецепта (тот же GET, тот же кэш). */
  ttsAudio(text: string): Observable<Blob> {
    return this.http.get(`${API_BASE}/tts`, { params: { text, r: TTS_REV }, responseType: 'blob' });
  }

  getPreferences(): Observable<FoodPreferences> {
    return this.http.get<FoodPreferences>(`${API_BASE}/preferences`);
  }
  /** Частичная правка: не переданные поля сервер оставляет как есть. */
  setPreferences(prefs: FoodPrefsPatch): Observable<FoodPreferences> {
    return this.http.put<FoodPreferences>(`${API_BASE}/preferences`, prefs);
  }

  getSettings(): Observable<AppSettings> {
    return this.http.get<AppSettings>(`${API_BASE}/settings`);
  }
  putSettings(models: ModelDefaults): Observable<AppSettings> {
    return this.http.put<AppSettings>(`${API_BASE}/settings`, { models });
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>(`${API_BASE}/plans`);
  }

  // Блюда всех принятых планов (свежие планы первыми) с флагом избранного.
  listRecipes(): Observable<RecipeItem[]> {
    return this.http.get<RecipeItem[]>(`${API_BASE}/recipes`);
  }
  setFavorite(r: Pick<RecipeItem, 'name' | 'planId' | 'dishId'>, favorite: boolean): Observable<{ key: string; favorite: boolean }> {
    return this.http.put<{ key: string; favorite: boolean }>(`${API_BASE}/recipes/favorite`, {
      name: r.name,
      favorite,
      planId: r.planId,
      dishId: r.dishId,
    });
  }

  // Выбранный «текущий» план (общий для покупок/готовки, хранится на сервере).
  getCurrentPlan(): Observable<{ planId: string | null }> {
    return this.http.get<{ planId: string | null }>(`${API_BASE}/current-plan`);
  }
  setCurrentPlan(planId: string | null): Observable<{ planId: string | null }> {
    return this.http.put<{ planId: string | null }>(`${API_BASE}/current-plan`, { planId });
  }

  getPlan(planId: string): Observable<WeekPlan> {
    return this.http.get<WeekPlan>(`${API_BASE}/plans/${planId}`);
  }

  // Полный план со всеми шагами (догенерирует недостающие) — для экспорта в PDF.
  // recipeModel пусто → бэк берёт модель «Рецепты» по умолчанию из настроек.
  fullPlan(planId: string, recipeModel: RecipeModel | '' = ''): Observable<WeekPlan> {
    return this.http.post<WeekPlan>(`${API_BASE}/plans/${planId}/full`, { recipeModel });
  }

  setStatus(planId: string, status: PlanStatus): Observable<WeekPlan> {
    return this.http.post<WeekPlan>(`${API_BASE}/plans/${planId}/status`, { status });
  }

  // Переименовать план (долгое нажатие на заголовок на странице плана).
  renamePlan(planId: string, title: string): Observable<WeekPlan> {
    return this.http.patch<WeekPlan>(`${API_BASE}/plans/${planId}`, { title });
  }

  deletePlan(planId: string): Observable<void> {
    return this.http.delete<void>(`${API_BASE}/plans/${planId}`);
  }

  // Покупки по рецептам: ингредиенты каждого блюда отдельно (без нормализации моделью).
  shoppingByDish(planId: string): Observable<DishShopping[]> {
    return this.http.get<DishShopping[]>(`${API_BASE}/plans/${planId}/shopping-list/by-dish`);
  }

  shoppingList(planId: string): Observable<ShoppingGroup[]> {
    return this.http.get<ShoppingGroup[]>(`${API_BASE}/plans/${planId}/shopping-list`);
  }

  // «↻ Перегенерировать» список покупок: нормализация мимо кэша с учётом обсуждения.
  regenerateShopping(planId: string, recipeModel: RecipeModel | string): Observable<ShoppingGroup[]> {
    return this.http.post<ShoppingGroup[]>(
      `${API_BASE}/plans/${planId}/shopping-list/regenerate`,
      { recipeModel },
    );
  }

  // Реплика в режиме «Обсуждение» (рецепт / план готовки / покупки). Версий плана не создаёт.
  discuss(body: {
    conversationId: string | null;
    planId: string;
    target: DiscussTarget;
    dishId?: string;
    message: string;
    recipeModel: RecipeModel;
  }): Observable<DiscussResponse> {
    return this.http.post<DiscussResponse>(`${API_BASE}/chat/discuss`, {
      ...body,
      gender: this.prefs.gender(),
    });
  }

  // Все сгенерированные варианты рецепта блюда (по моделям) — для сравнения.
  dishVariants(planId: string, dishId: string): Observable<DishVariant[]> {
    return this.http.get<DishVariant[]>(
      `${API_BASE}/plans/${planId}/dishes/${dishId}/variants`,
    );
  }

  // Единый план готовки на весь план (ленивая генерация, кэш). action: open | select.
  cookingPlan(
    planId: string,
    recipeModel: RecipeModel | string,
    action: VariantAction = 'open',
  ): Observable<CookingPlan> {
    return this.http.post<CookingPlan>(`${API_BASE}/plans/${planId}/cooking`, {
      recipeModel,
      action,
    });
  }

  // Варианты плана готовки по моделям — для сравнения.
  cookingVariants(planId: string): Observable<CookingPlanVariant[]> {
    return this.http.get<CookingPlanVariant[]>(`${API_BASE}/plans/${planId}/cooking/variants`);
  }

  // action: open — активный вариант (сгенерит первый, если детали нет);
  // select — сделать recipeModel активным (сгенерит его вариант, если ещё нет).
  dishDetails(
    planId: string,
    dishId: string,
    recipeModel: RecipeModel | string,
    action: VariantAction = 'open',
  ): Observable<Dish> {
    return this.http.post<Dish>(
      `${API_BASE}/plans/${planId}/dishes/${dishId}/details`,
      { recipeModel, action },
    );
  }

  conversationMessages(conversationId: string): Observable<ChatMessage[]> {
    return this.http.get<ChatMessage[]>(
      `${API_BASE}/conversations/${conversationId}/messages`,
    );
  }

  searchMessages(q: string): Observable<MessageSearchHit[]> {
    return this.http.get<MessageSearchHit[]>(`${API_BASE}/messages/search`, {
      params: { q },
    });
  }

  // Оценка 👍/👎 ответа модели. vote: 1 | -1. Возврат — текущее состояние (1|-1|0).
  rate(body: RatingBody): Observable<RatingState> {
    return this.http.post<RatingState>(`${API_BASE}/ratings`, body);
  }
  // Каталог причин 👎 — один раз на сессию (кэш в shareReplay).
  private reasons$?: Observable<RatingReasonCatalog>;
  ratingReasons(): Observable<RatingReasonCatalog> {
    this.reasons$ ??= this.http
      .get<RatingReasonCatalog>(`${API_BASE}/ratings/reasons`)
      .pipe(shareReplay({ bufferSize: 1, refCount: false }));
    return this.reasons$;
  }
  // Причины к уже поставленному 👎 (PATCH — голос не трогаем).
  setRatingReasons(body: {
    targetType: RatingTarget;
    targetId: string;
    model: string;
    reasons: string[];
    note: string;
  }): Observable<RatingState> {
    return this.http.patch<RatingState>(`${API_BASE}/ratings/reasons`, body);
  }
  rating(targetType: string, targetId: string, model: string): Observable<RatingState> {
    return this.http.get<RatingState>(`${API_BASE}/ratings`, {
      params: { targetType, targetId, model },
    });
  }
}
