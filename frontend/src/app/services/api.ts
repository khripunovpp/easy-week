import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { Dish, PlanStatus, WeekPlan } from '../models/plan.model';

// Дев: бэкенд на :8000. В проде (одна раздача) заменить на относительный '/api'.
const API_BASE = 'http://127.0.0.1:8000/api';

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

  chat(message: string, conversationId: string | null): Observable<ChatResponse> {
    return this.http.post<ChatResponse>(`${API_BASE}/chat`, { message, conversationId });
  }

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>(`${API_BASE}/plans`);
  }

  getPlan(planId: string): Observable<WeekPlan> {
    return this.http.get<WeekPlan>(`${API_BASE}/plans/${planId}`);
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
}
