import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ChatMessage, Dish, PlanStatus, WeekPlan } from '../models/plan.model';

// Относительный путь: в проде nginx проксирует /api → бэкенд;
// в деве — dev-прокси Angular (proxy.conf.json) на localhost:8000.
const API_BASE = '/api';

export interface ChatResponse {
  conversationId: string;
  reply: string;
  plan: WeekPlan | null;
}

export interface PlanSummary {
  id: string;
  title: string;
  weekLabel: string;
  status: PlanStatus;
  dishesCount: number;
  emoji: string;
}

export interface ShoppingGroup {
  category: string;
  items: { name: string; qty: number; unit: string; category: string }[];
}

@Injectable({ providedIn: 'root' })
export class EasyWeekApi {
  private readonly http = inject(HttpClient);

  chat(
    message: string,
    conversationId: string | null,
    dishesCount = 5,
  ): Observable<ChatResponse> {
    return this.http.post<ChatResponse>(`${API_BASE}/chat`, {
      message,
      conversationId,
      dishesCount,
    });
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>(`${API_BASE}/plans`);
  }

  getPlan(planId: string): Observable<WeekPlan> {
    return this.http.get<WeekPlan>(`${API_BASE}/plans/${planId}`);
  }

  // Полный план со всеми шагами (догенерирует недостающие) — для экспорта в PDF.
  fullPlan(planId: string): Observable<WeekPlan> {
    return this.http.post<WeekPlan>(`${API_BASE}/plans/${planId}/full`, {});
  }

  setStatus(planId: string, status: PlanStatus): Observable<WeekPlan> {
    return this.http.post<WeekPlan>(`${API_BASE}/plans/${planId}/status`, { status });
  }

  shoppingList(planId: string): Observable<ShoppingGroup[]> {
    return this.http.get<ShoppingGroup[]>(`${API_BASE}/plans/${planId}/shopping-list`);
  }

  dishDetails(planId: string, dishId: string): Observable<Dish> {
    return this.http.post<Dish>(`${API_BASE}/plans/${planId}/dishes/${dishId}/details`, {});
  }

  conversationMessages(conversationId: string): Observable<ChatMessage[]> {
    return this.http.get<ChatMessage[]>(
      `${API_BASE}/conversations/${conversationId}/messages`,
    );
  }
}
