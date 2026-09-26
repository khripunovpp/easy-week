import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
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
}
export interface CookingPlanVariant {
  model: string;
  provider: string;
  steps: CookingStep[];
  note: string;
}
import { Preferences, RecipeModel } from './preferences';
import { AuthService } from './auth';

// Относительный путь: в проде nginx проксирует /api → бэкенд;
// в деве — dev-прокси Angular (proxy.conf.json) на localhost:8000.
const API_BASE = '/api';

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

export type RatingTarget = 'recipe' | 'plan' | 'cooking' | 'message';
export interface RatingBody {
  targetType: RatingTarget;
  targetId: string;
  model: string;
  vote: 1 | -1;
  planId?: string;
  dishId?: string;
  conversationId?: string;
}

export interface ChatStreamMeta {
  conversationId: string;
  planId: string;
  title: string;
  weekLabel: string;
  reply: string;
  provider?: string;
}

export interface ChatStreamHandlers {
  onMeta: (meta: ChatStreamMeta) => void;
  onDish: (dish: Dish) => void;
  onDone: (info: { planId: string; dishesCount: number; messageId?: string; model?: string }) => void;
  onError: (message: string) => void;
}

export interface ShoppingListItem {
  name: string;
  qty: number;
  unit: string;
  category: string;
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
 *  вручную; suggestedAllergies — подозрения из чата («Добавить в аллергии?»). */
export interface FoodPreferences extends FoodPrefs {
  allergies: string[];
  suggestedAllergies: string[];
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
          handlers.onDone(
            payload as { planId: string; dishesCount: number; messageId?: string; model?: string },
          );
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

  limits(): Observable<LimitsStatus> {
    return this.http.get<LimitsStatus>(`${API_BASE}/limits`);
  }

  getPreferences(): Observable<FoodPreferences> {
    return this.http.get<FoodPreferences>(`${API_BASE}/preferences`);
  }
  /** Частичная правка: не переданные поля сервер оставляет как есть. */
  setPreferences(prefs: FoodPrefsPatch): Observable<FoodPreferences> {
    return this.http.put<FoodPreferences>(`${API_BASE}/preferences`, prefs);
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>(`${API_BASE}/plans`);
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
  fullPlan(planId: string, recipeModel: RecipeModel): Observable<WeekPlan> {
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
  rate(body: RatingBody): Observable<{ vote: number }> {
    return this.http.post<{ vote: number }>(`${API_BASE}/ratings`, body);
  }
  rating(targetType: string, targetId: string, model: string): Observable<{ vote: number }> {
    return this.http.get<{ vote: number }>(`${API_BASE}/ratings`, {
      params: { targetType, targetId, model },
    });
  }
}
