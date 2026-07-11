import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'chat' },
  {
    path: 'chat',
    loadComponent: () => import('./features/chat/chat').then((m) => m.Chat),
  },
  {
    path: 'plan/:planId/dish/:dishId',
    loadComponent: () => import('./features/dish/dish').then((m) => m.DishPage),
  },
  {
    path: 'plans',
    loadComponent: () => import('./features/plans/plans').then((m) => m.Plans),
  },
  {
    path: 'shopping',
    loadComponent: () => import('./features/shopping/shopping').then((m) => m.Shopping),
  },
  {
    path: 'profile',
    loadComponent: () => import('./features/stub/stub').then((m) => m.Stub),
    data: { title: 'Профиль', emoji: '🧑‍🍳' },
  },
  { path: '**', redirectTo: 'chat' },
];
