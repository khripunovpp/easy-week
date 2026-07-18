import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ChatMessage, Dish, PlanStatus, WeekPlan } from '../models/plan.model';
import { Preferences, RecipeModel } from './preferences';

// Относительный путь: в проде nginx проксирует /api → бэкенд;
// в деве — dev-прокси Angular (proxy.conf.json) на localhost:8000.
const API_BASE = '/api';

export interface ChatResponse {
  conversationId: string;
  reply: string;
  plan: WeekPlan | null;
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
  onDone: (info: { planId: string; dishesCount: number }) => void;
  onError: (message: string) => void;
}

export interface ShoppingListItem {
  name: string;
  qty: number;
  unit: string;
  category: string;
}

export interface ShoppingStreamHandlers {
  onItem: (item: ShoppingListItem) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

export interface FoodPrefs {
  dislikes: string[];
  likes: string[];
}

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
}

export interface ShoppingGroup {
  category: string;
  items: { name: string; qty: number; unit: string; category: string }[];
}

@Injectable({ providedIn: 'root' })
export class EasyWeekApi {
  private readonly http = inject(HttpClient);
  private readonly prefs = inject(Preferences);

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
          handlers.onDone(payload as { planId: string; dishesCount: number });
        else if (event === 'error')
          handlers.onError((payload as { message?: string }).message ?? 'Ошибка генерации');
      },
    );
  }

  // Потоковый список покупок (SSE): item (по одному) → done.
  async shoppingStream(planId: string, handlers: ShoppingStreamHandlers): Promise<void> {
    await this.openSse(
      `${API_BASE}/plans/${planId}/shopping-list/stream`,
      { method: 'GET' },
      handlers.onError,
      (event, payload) => {
        if (event === 'item') handlers.onItem(payload as ShoppingListItem);
        else if (event === 'done') handlers.onDone();
        else if (event === 'error')
          handlers.onError((payload as { message?: string }).message ?? 'Ошибка');
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
      resp = await fetch(url, init);
    } catch {
      onError('Нет связи с сервером');
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

  getPreferences(): Observable<FoodPrefs> {
    return this.http.get<FoodPrefs>(`${API_BASE}/preferences`);
  }
  setPreferences(prefs: FoodPrefs): Observable<FoodPrefs> {
    return this.http.put<FoodPrefs>(`${API_BASE}/preferences`, prefs);
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>(`${API_BASE}/plans`);
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

  deletePlan(planId: string): Observable<void> {
    return this.http.delete<void>(`${API_BASE}/plans/${planId}`);
  }

  shoppingList(planId: string): Observable<ShoppingGroup[]> {
    return this.http.get<ShoppingGroup[]>(`${API_BASE}/plans/${planId}/shopping-list`);
  }

  // action: open — активный вариант (сгенерит первый, если детали нет);
  // select — сделать recipeModel активным (сгенерит его вариант, если ещё нет).
  dishDetails(
    planId: string,
    dishId: string,
    recipeModel: RecipeModel | string,
    action: 'open' | 'select' = 'open',
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
}
