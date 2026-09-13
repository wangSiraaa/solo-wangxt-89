import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', loadComponent: () =>
    import('./pages/events-page.component').then((m) => m.EventsPageComponent) },
  { path: 'event/:id/review', loadComponent: () =>
    import('./pages/review-page.component').then((m) => m.ReviewPageComponent) },
  { path: 'event/:id/timeline', loadComponent: () =>
    import('./pages/timeline-page.component').then((m) => m.TimelinePageComponent) },
  { path: 'event/:id/leaderboard', loadComponent: () =>
    import('./pages/leaderboard-page.component').then((m) => m.LeaderboardPageComponent) },
  { path: 'event/:id/clock', loadComponent: () =>
    import('./pages/clock-page.component').then((m) => m.ClockPageComponent) },
  { path: 'event/:id/history', loadComponent: () =>
    import('./pages/history-page.component').then((m) => m.HistoryPageComponent) },
];
