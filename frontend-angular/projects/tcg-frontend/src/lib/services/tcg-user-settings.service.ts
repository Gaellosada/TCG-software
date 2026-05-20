import { Injectable, computed, effect, signal } from '@angular/core';

export type TcgTheme = 'light' | 'dark';
export type TcgChartType = 'line' | 'candlestick';

export const TCG_LS_KEYS = {
  theme: 'tcg-theme',
  chartType: 'tcg-default-chart-type',
  riskFreeRate: 'tcg-risk-free-rate',
  sidebarCollapsed: 'tcg-sidebar-collapsed',
} as const;

export const TCG_DEFAULT_RISK_FREE_RATE_PCT = 4.0;
export const TCG_DEFAULT_RISK_FREE_RATE_FRACTION = 0.04;

/**
 * Reads + writes user preferences shared across the library. Mirrors React's
 * `useTheme` + `useChartPreference` + `lib/userSettings.js` trio:
 *
 *   - `theme` / `chartType` signals replace the `<html data-*>` round-trip
 *     and the `MutationObserver` watcher;
 *   - `setTheme` / `setChartType` persist to localStorage AND mirror the
 *     value into `<html data-theme>` / `<html data-chart-type>` for CSS
 *     that selects on those attributes (preserves cross-build parity with
 *     the React app);
 *   - `getRiskFreeRateFraction()` converts the stored percent string to a
 *     fraction (single conversion site — the wire contract is fractions).
 *
 * G5: NOT `providedIn: 'root'`. Provide via the `tcgRoutes` parent route
 * (`providers: [TcgUserSettingsService]`) so every page in the library
 * shares one instance scoped to the feature.
 */
@Injectable()
export class TcgUserSettingsService {
  private readonly _theme = signal<TcgTheme>(this.readTheme());
  private readonly _chartType = signal<TcgChartType>(this.readChartType());
  private readonly _rfPct = signal<string>(this.readRfPctRaw());
  private readonly _sidebarCollapsed = signal<boolean>(this.readSidebarCollapsed());

  /** Current theme. */
  readonly theme = this._theme.asReadonly();
  /** Default chart type. */
  readonly chartType = this._chartType.asReadonly();
  /** Risk-free rate as stored (string percent, e.g. `"4.00"`). */
  readonly riskFreeRatePct = this._rfPct.asReadonly();
  /** Sidebar collapsed flag, mirrored from localStorage. */
  readonly sidebarCollapsed = this._sidebarCollapsed.asReadonly();

  /** Risk-free rate as an annualised fraction (`0.04` for 4%). */
  readonly riskFreeRateFraction = computed(() => {
    const raw = this._rfPct();
    if (raw == null || raw === '') return TCG_DEFAULT_RISK_FREE_RATE_FRACTION;
    const pct = parseFloat(raw);
    if (!Number.isFinite(pct) || pct < 0) return TCG_DEFAULT_RISK_FREE_RATE_FRACTION;
    return pct / 100;
  });

  constructor() {
    // Mirror theme + chart type onto <html data-*> so CSS selectors on the
    // host page continue to work without rewriting every selector. The
    // React app was the source of truth for those attributes; this keeps
    // shared deployments interchangeable.
    effect(() => {
      const theme = this._theme();
      try {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem(TCG_LS_KEYS.theme, theme);
      } catch {
        /* localStorage may be unavailable (SSR, locked-down browsers). */
      }
    });
    effect(() => {
      const ct = this._chartType();
      try {
        document.documentElement.setAttribute('data-chart-type', ct);
        localStorage.setItem(TCG_LS_KEYS.chartType, ct);
      } catch {
        /* ignore */
      }
    });
    effect(() => {
      const v = this._rfPct();
      try {
        localStorage.setItem(TCG_LS_KEYS.riskFreeRate, v);
      } catch {
        /* ignore */
      }
    });
    effect(() => {
      const v = this._sidebarCollapsed();
      try {
        localStorage.setItem(TCG_LS_KEYS.sidebarCollapsed, v ? 'true' : 'false');
      } catch {
        /* ignore */
      }
    });
  }

  setTheme(theme: TcgTheme): void {
    this._theme.set(theme);
  }
  setChartType(ct: TcgChartType): void {
    this._chartType.set(ct);
  }
  setRiskFreeRatePct(pct: string): void {
    this._rfPct.set(pct);
  }
  setSidebarCollapsed(collapsed: boolean): void {
    this._sidebarCollapsed.set(collapsed);
  }

  // ─────────────────────────────────────────────────────────────────────
  // localStorage readers (defensive — keys may be absent / corrupted)
  // ─────────────────────────────────────────────────────────────────────

  private readTheme(): TcgTheme {
    try {
      const raw = localStorage.getItem(TCG_LS_KEYS.theme);
      return raw === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  }

  private readChartType(): TcgChartType {
    try {
      const raw = localStorage.getItem(TCG_LS_KEYS.chartType);
      return raw === 'candlestick' ? 'candlestick' : 'line';
    } catch {
      return 'line';
    }
  }

  private readRfPctRaw(): string {
    try {
      const raw = localStorage.getItem(TCG_LS_KEYS.riskFreeRate);
      if (raw == null || raw === '') return String(TCG_DEFAULT_RISK_FREE_RATE_PCT);
      return raw;
    } catch {
      return String(TCG_DEFAULT_RISK_FREE_RATE_PCT);
    }
  }

  private readSidebarCollapsed(): boolean {
    try {
      return localStorage.getItem(TCG_LS_KEYS.sidebarCollapsed) === 'true';
    } catch {
      return false;
    }
  }
}
