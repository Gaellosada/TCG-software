import { Routes } from '@angular/router';
import { TcgPlaceholderRouteComponent } from './components/placeholder-page/tcg-placeholder-route.component';
import { TcgSettingsPageComponent } from './pages/settings/tcg-settings-page.component';
import { TcgUserSettingsService } from './services/tcg-user-settings.service';

/**
 * Library `Routes` array. Mirrors React's `<Routes>` in `App.jsx`.
 *
 * G3: NO `provideRouter()` / `RouterModule.forRoot()` here. Hosts spread
 * `tcgRoutes` into their own router config; the dev-harness does this via
 * its `app.routes.ts`.
 *
 * G5: `TcgUserSettingsService` is feature-scoped via the empty-path
 * parent route's `providers` array, so every page in the library shares
 * one instance without leaking root-scoping.
 *
 * Phase A scaffold: every page currently renders the generic
 * `TcgPlaceholderRouteComponent` (the title / description come from
 * `route.data`). Workers porting Data, Settings, etc. swap each
 * `component` to the real standalone page component.
 */
export const tcgRoutes: Routes = [
  {
    path: '',
    providers: [TcgUserSettingsService],
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'data' },
      {
        path: 'data',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Data', description: 'Data page port lands in Wave I Phase B (Worker B).' },
      },
      { path: 'settings', component: TcgSettingsPageComponent },
      {
        path: 'indicators',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Indicators', description: 'Port pending in a later wave.' },
      },
      {
        path: 'signals',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Signals', description: 'Port pending in a later wave.' },
      },
      {
        path: 'portfolio',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Portfolio', description: 'Port pending in a later wave.' },
      },
      {
        path: 'help',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Help', description: 'Port pending in a later wave.' },
      },
      {
        path: 'running-signals',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Running Signals', description: 'Live page — incoming work.' },
      },
      {
        path: 'mongodb-agent',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'MongoDB Agent', description: 'Agents page — incoming work.' },
      },
      {
        path: 'tickets',
        component: TcgPlaceholderRouteComponent,
        data: { title: 'Tickets', description: 'Ticketing page — incoming work.' },
      },
    ],
  },
];
