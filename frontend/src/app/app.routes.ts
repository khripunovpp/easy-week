import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'home' },
  {
    path: 'home',
    loadComponent: () => import('./features/home/home').then((m) => m.Home),
  },
  {
    path: 'chat',
    loadComponent: () => import('./features/chat/chat').then((m) => m.Chat),
  },
  {
    path: 'plan/:id',
    loadComponent: () => import('./features/plan/plan').then((m) => m.PlanPage),
  },
  {
    path: 'plan/:planId/dish/:dishId',
    loadComponent: () => import('./features/dish/dish').then((m) => m.DishPage),
  },
  {
    path: 'plan/:planId/dish/:dishId/compare',
    loadComponent: () => import('./features/compare/compare').then((m) => m.ComparePage),
  },
  {
    path: 'print/:planId',
    loadComponent: () => import('./features/print/print').then((m) => m.PrintPage),
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
    path: 'shopping/:planId',
    loadComponent: () => import('./features/shopping/shopping').then((m) => m.Shopping),
  },
  {
    path: 'profile',
    loadComponent: () => import('./features/profile/profile').then((m) => m.ProfilePage),
  },
  { path: '**', redirectTo: 'home' },
];
