import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TcgIconComponent } from '../../components/icon/tcg-icon.component';
import { TcgPageContainerComponent } from '../../layout/tcg-page-container.component';
import { TcgRfrInputComponent } from '../../components/risk-free-rate-input/tcg-rfr-input.component';
import {
  TcgChartType,
  TcgTheme,
  TcgUserSettingsService,
} from '../../services/tcg-user-settings.service';

/**
 * Settings page. Mirrors React `SettingsPage.jsx`:
 *   - Theme picker (`dark` / `light`)
 *   - Default chart-type picker (`candlestick` / `line`)
 *   - Default risk-free-rate percent input
 *
 * State + persistence is fully delegated to the feature-scoped
 * `TcgUserSettingsService` (provided via `tcgRoutes[0].providers`). The
 * service owns the localStorage round-trip for the four shared keys
 * (`tcg-theme`, `tcg-default-chart-type`, `tcg-risk-free-rate`,
 * `tcg-sidebar-collapsed`) so React and Angular builds remain
 * cross-deployable.
 *
 * Validation rule preserved from React: the risk-free-rate value is only
 * persisted when it parses as a non-negative finite number. Non-numeric
 * or negative input is shown in the field (transient typing state) but
 * not committed to the service / localStorage. This matches the React
 * page's TC4.8 behaviour.
 *
 * G5: this component does NOT provide `TcgUserSettingsService` itself —
 * it injects the feature-scoped instance shared by every page in the
 * library. G8: standalone. OnPush change detection.
 */
@Component({
  selector: 'tcg-settings-page',
  standalone: true,
  imports: [CommonModule, TcgIconComponent, TcgPageContainerComponent, TcgRfrInputComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-settings-page.component.html',
  styleUrls: ['./tcg-settings-page.component.css'],
})
export class TcgSettingsPageComponent {
  private readonly userSettings = inject(TcgUserSettingsService);

  /** Current theme — read from the shared service. */
  readonly theme = this.userSettings.theme;
  /** Current default chart type — read from the shared service. */
  readonly chartType = this.userSettings.chartType;

  /**
   * Local mirror of the risk-free-rate input string. Seeded from the
   * shared service and re-synced whenever the service value changes
   * externally (e.g. another tab / page writes via the service). Invalid
   * typed values land here but are NOT propagated to the service,
   * preserving React's TC4.8 contract (negative / non-numeric input is
   * displayed transiently but not persisted).
   */
  private readonly _rfPctLocal = signal<string>(this.userSettings.riskFreeRatePct());

  /** Risk-free-rate percent string currently displayed in the input. */
  readonly rfPct = this._rfPctLocal.asReadonly();

  /** Whether the current theme equals `'dark'`. */
  readonly isDark = computed(() => this.theme() === 'dark');
  /** Whether the current theme equals `'light'`. */
  readonly isLight = computed(() => this.theme() === 'light');
  /** Whether the current default chart type equals `'candlestick'`. */
  readonly isCandlestick = computed(() => this.chartType() === 'candlestick');
  /** Whether the current default chart type equals `'line'`. */
  readonly isLine = computed(() => this.chartType() === 'line');

  setTheme(theme: TcgTheme): void {
    this.userSettings.setTheme(theme);
  }

  setChartType(ct: TcgChartType): void {
    this.userSettings.setChartType(ct);
  }

  /**
   * Mirrors React's `handleRfChange`. The input event fires for every
   * keystroke (`'4'`, `'4.'`, `'4.5'`, ...). We only commit the value to
   * the shared service / localStorage when it parses as a non-negative
   * finite number — the same validation gate React uses. Otherwise the
   * field carries the raw typed string locally without persisting.
   *
   * REVIEW: because the current `TcgUserSettingsService.setRiskFreeRatePct`
   * effect writes any string straight to localStorage, we route invalid
   * values into a transient local state by NOT calling the setter. The
   * net behaviour matches React: a transient `'-1'` displays but never
   * lands in localStorage.
   */
  onRfChange(raw: string): void {
    // Always update the local mirror so the input reflects what the
    // user typed (matches React's setRfPct(value) outside the validity
    // gate).
    this._rfPctLocal.set(raw);
    const pct = parseFloat(raw);
    if (!Number.isFinite(pct) || pct < 0) {
      return;
    }
    this.userSettings.setRiskFreeRatePct(raw);
  }
}
