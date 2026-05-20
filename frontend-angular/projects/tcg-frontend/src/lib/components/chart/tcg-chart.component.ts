import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  Optional,
  SimpleChanges,
  ViewChild,
  effect,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { CHART_CONFIG, TcgTheme, buildBaseLayout } from './chart-theme';
import { TcgChartMarker, buildAllMarkerTraces } from './chart-markers';
import { TcgCsvTrace, buildCsv, downloadCsv } from './chart-csv';
import { TcgPlotlyModule, TcgPlotlyService } from './tcg-plotly.service';
import { TcgUserSettingsService } from '../../services/tcg-user-settings.service';

// Plotly's built-in `disk` icon — duplicated as a small POJO so this module
// doesn't import `plotly.js` directly (which would pull the full plotly
// source into the optimizer).
const DISK_ICON = {
  width: 857.1,
  height: 1000,
  path:
    'm214-7h429v214h-429v-214z m500 0h72v500q0 8-6 21t-11 20l-157 156q-5 6-19 12t-22 5v-232q0-22-15-38t-38-16h-322q-22 0-37 16t-16 38v232h-72v-714h72v232q0 22 16 38t37 16h465q22 0 38-16t15-38v-232z m-214 518v178q0 8-5 13t-13 5h-107q-7 0-13-5t-5-13v-178q0-8 5-13t13-5h107q7 0 13 5t5 13z m357-18v-518q0-22-15-38t-38-16h-750q-23 0-38 16t-16 38v750q0 22 16 38t38 16h517q23 0 50-12t42-26l156-157q16-15 27-42t11-49z',
  transform: 'matrix(1 0 0 -1 0 850)',
};

/**
 * Shared Plotly chart wrapper. Mirrors the React `Chart.jsx`:
 *   - lazy-loads Plotly on `ngAfterViewInit`;
 *   - calls `Plotly.newPlot` on first mount and `Plotly.react` on
 *     subsequent input changes (preserves zoom/pan state);
 *   - reads theme from `TcgUserSettingsService` when supplied; falls back
 *     to dark otherwise (matches React's default);
 *   - injects a CSV-export modebar button (visible-traces-only export);
 *   - when `markers` is non-empty, appends marker traces; identity-stable
 *     pass-through when empty (this invariant is load-bearing).
 *
 * G5: provides its own `TcgPlotlyService` instance via `providers: [...]`.
 */
@Component({
  selector: 'tcg-chart',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [TcgPlotlyService],
  template: ` <div #host class="tcg-chart__host" [class]="className || ''"></div> `,
  styles: [
    `
      :host {
        display: block;
        width: 100%;
        height: 100%;
      }
      .tcg-chart__host {
        width: 100%;
        height: 100%;
      }
    `,
  ],
})
export class TcgChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  @ViewChild('host', { static: true }) hostRef!: ElementRef<HTMLDivElement>;

  @Input({ required: true }) traces!: ReadonlyArray<Record<string, unknown>>;
  @Input() layoutOverrides?: Record<string, unknown>;
  @Input() markers?: ReadonlyArray<TcgChartMarker>;
  @Input() markerHovertemplates?: { sell?: string; buy?: string };
  @Input() downloadFilename: string = 'chart';
  @Input() className?: string;

  private readonly plotly = inject(TcgPlotlyService);
  private readonly userSettings = inject(TcgUserSettingsService, { optional: true });

  private plotlyMod: TcgPlotlyModule | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private mounted = false;

  constructor(@Optional() _settingsForOnChanges?: TcgUserSettingsService) {
    // Re-render when theme changes (keeps trace colors in sync).
    effect(() => {
      // Subscribe to theme so the effect re-runs.
      this.currentTheme();
      if (this.mounted) {
        // Fire-and-forget — failures here would only mean a stale theme
        // until the next input change.
        this.render().catch(() => {
          /* swallow — see effect docstring */
        });
      }
    });
  }

  async ngAfterViewInit(): Promise<void> {
    this.plotlyMod = await this.plotly.load();
    await this.render();
    this.mounted = true;
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.plotlyMod && this.hostRef.nativeElement) {
          this.plotlyMod.Plots.resize(this.hostRef.nativeElement);
        }
      });
      this.resizeObserver.observe(this.hostRef.nativeElement);
    }
  }

  async ngOnChanges(_changes: SimpleChanges): Promise<void> {
    if (this.mounted) {
      await this.render();
    }
  }

  ngOnDestroy(): void {
    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
    if (this.plotlyMod && this.hostRef && this.hostRef.nativeElement) {
      try {
        this.plotlyMod.purge(this.hostRef.nativeElement);
      } catch {
        /* ignore */
      }
    }
  }

  private currentTheme(): TcgTheme {
    return this.userSettings?.theme() ?? 'dark';
  }

  private async render(): Promise<void> {
    if (!this.plotlyMod || !this.hostRef?.nativeElement) return;
    const theme = this.currentTheme();
    const layout = buildBaseLayout(this.layoutOverrides ?? {}, theme);
    const markerTraces = buildAllMarkerTraces(this.markers ?? null, theme, {
      hovertemplates: this.markerHovertemplates,
    });
    const plotData =
      markerTraces.length === 0
        ? (this.traces as Array<Record<string, unknown>>)
        : [...this.traces, ...markerTraces];

    const config: Record<string, unknown> = {
      ...CHART_CONFIG,
      modeBarButtonsToAdd: [
        {
          name: 'downloadCsv',
          title: 'Download visible series as CSV',
          icon: DISK_ICON,
          click: (gd: { data?: unknown[] } | null) => {
            const traceList = (gd?.data ?? plotData) as ReadonlyArray<TcgCsvTrace>;
            const csv = buildCsv(traceList);
            if (!csv) return;
            downloadCsv(csv, this.downloadFilename);
          },
        },
      ],
    };

    if (!this.mounted) {
      await this.plotlyMod.newPlot(this.hostRef.nativeElement, plotData, layout, config);
    } else {
      await this.plotlyMod.react(this.hostRef.nativeElement, plotData, layout, config);
    }
  }
}
