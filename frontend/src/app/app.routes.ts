import { Routes } from '@angular/router';
import { authGuard, loginGuard } from './services/auth-guard';

// Все экраны — под authGuard (вход по общему паролю). /login — снаружи.
const appRoutes: Routes = [
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
    path: 'cooking',
    loadComponent: () => import('./features/cooking/cooking').then((m) => m.CookingPlanPage),
  },
  {
    path: 'cooking/:planId',
    loadComponent: () => import('./features/cooking/cooking').then((m) => m.CookingPlanPage),
  },
  {
    path: 'cooking/:planId/compare',
    loadComponent: () =>
      import('./features/cooking/cooking-compare').then((m) => m.CookingComparePage),
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
  {
    // Под-экран профиля: модели по умолчанию по задачам (общие, на сервере).
    path: 'settings/models',
    loadComponent: () =>
      import('./features/settings-models/settings-models').then((m) => m.SettingsModelsPage),
  },
  {
    // Пищевые предпочтения: аллергии, любит / не любит, БЖУ (ссылка — из профиля)
    path: 'preferences',
    loadComponent: () =>
      import('./features/preferences/preferences').then((m) => m.PreferencesPage),
  },
  { path: '**', redirectTo: 'home' },
];

export const routes: Routes = [
  {
    path: 'login',
    canActivate: [loginGuard],
    loadComponent: () => import('./features/login/login').then((m) => m.LoginPage),
  },
  // Бескомпонентный родитель: гард срабатывает на каждую навигацию внутри (статус кэширован).
  { path: '', canActivateChild: [authGuard], children: appRoutes },
];
