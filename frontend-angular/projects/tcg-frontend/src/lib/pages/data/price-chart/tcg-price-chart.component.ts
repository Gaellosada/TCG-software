import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  Input,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { TcgChartComponent } from '../../../components/chart/tcg-chart.component';
import { TRACE_COLORS, getChartColors } from '../../../components/chart/chart-theme';
import {
  TcgDataApi,
  TcgPriceSeries,
} from '../../../api/tcg-data-api.service';
import { TcgUserSettingsService } from '../../../services/tcg-user-settings.service';
import { tcgFormatDateInt, tcgPrepareChartData } from '../data-format';

/**
 * Price chart for a single (collection, instrument) pair. Mirrors React's
 * `pages/Data/PriceChart.jsx`. OHLC bars get a candlestick trace when
 * the data passes the validity heuristic; non-OHLC instruments render as
 * a line plot.
 *
 * G3/G4/G8: standalone, `tcg-` selector + `Tcg*` class.
 */
@Component({
  selector: 'tcg-price-chart',
  standalone: true,
  imports: [CommonModule, TcgChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-price-chart.component.html',
  styleUrls: ['../_chart-base.css', './tcg-price-chart.component.css'],
})
export class TcgPriceChartComponent {
  @Input({ required: true }) set collection(value: string) {
    this._collection.set(value);
  }
  get collection(): string {
    return this._collection();
  }
  @Input({ required: true }) set instrument(value: string) {
    this._instrument.set(value);
  }
  get instrument(): string {
    return this._instrument();
  }

  protected readonly _collection = signal('');
  protected readonly _instrument = signal('');

  private readonly dataApi = inject(TcgDataApi);
  private readonly userSettings = inject(TcgUserSettingsService, { optional: true });

  protected readonly data = signal<TcgPriceSeries | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<Error | null>(null);

  // Local chart-type override, seeded from user-settings preference. Mirrors
  // React's local state that syncs from `useChartPreference`.
  protected readonly chartType = signal<'line' | 'candlestick'>(
    this.userSettings?.chartType() ?? 'line',
  );

  constructor() {
    // Sync local state to global preference (mirrors React's useEffect on
    // `preference`).
    effect(() => {
      const pref = this.userSettings?.chartType();
      if (pref) this.chartType.set(pref);
    });

    // Fetch whenever collection/instrument changes.
    effect(() => {
      const coll = this._collection();
      const inst = this._instrument();
      if (!coll || !inst) return;
      this.loadPrices(coll, inst);
    });
  }

  private async loadPrices(collection: string, instrument: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const res = await firstValueFrom(
        this.dataApi.getInstrumentPrices(collection, instrument),
      );
      this.data.set(res);
    } catch (err: unknown) {
      this.error.set(err instanceof Error ? err : new Error(String(err)));
    } finally {
      this.loading.set(false);
    }
  }

  protected readonly prepared = computed(() => {
    const d = this.data();
    if (!d || !d.dates || d.dates.length === 0) return null;
    return tcgPrepareChartData(d);
  });

  protected readonly hasOHLC = computed(() => this.prepared()?.hasOHLC ?? false);

  protected readonly traces = computed<Array<Record<string, unknown>>>(() => {
    const data = this.data();
    const prepared = this.prepared();
    if (!data || !prepared) return [];
    const dates = data.dates.map((dt) => tcgFormatDateInt(dt));
    const effectiveType = prepared.hasOHLC ? this.chartType() : 'line';
    const colors = getChartColors(this.userSettings?.theme() ?? 'dark');
    const t: Array<Record<string, unknown>> = [];

    if (effectiveType === 'candlestick') {
      t.push({
        x: dates,
        open: prepared.open,
        high: prepared.high,
        low: prepared.low,
        close: prepared.close,
        type: 'candlestick',
        name: 'OHLC',
        increasing: { line: { color: '#10b981' } },
        decreasing: { line: { color: '#ef4444' } },
      });
    } else {
      t.push({
        x: dates,
        y: data.close,
        type: 'scatter',
        mode: 'lines',
        name: 'Close',
        line: { color: TRACE_COLORS[0], width: 1 },
        hovertemplate: '%{x}<br>Close: %{y:,.2f}<extra></extra>',
      });
    }

    if (prepared.hasVolume) {
      t.push({
        x: dates,
        y: data.volume,
        type: 'bar',
        name: 'Volume',
        yaxis: 'y2',
        marker: { color: colors.volumeBar },
        hovertemplate: '%{x}<br>Volume: %{y:,.0f}<extra></extra>',
      });
    }
    return t;
  });

  protected readonly layoutOverrides = computed<Record<string, unknown>>(() => {
    const prepared = this.prepared();
    const colors = getChartColors(this.userSettings?.theme() ?? 'dark');
    if (!prepared) return {};
    return {
      xaxis: prepared.hasVolume ? { anchor: 'y2' } : {},
      yaxis: {
        title: { text: 'Price', font: { size: 11, color: colors.secondaryFont } },
        domain: prepared.hasVolume ? [0.28, 1.0] : [0, 1.0],
      },
      ...(prepared.hasVolume
        ? {
            yaxis2: {
              domain: [0, 0.2],
              zeroline: false,
              showgrid: true,
              title: {
                text: 'Volume',
                font: { size: 11, color: colors.secondaryFont },
              },
              anchor: 'x',
            },
          }
        : {}),
    };
  });

  protected readonly displayMeta = computed(() => {
    const d = this.data();
    if (!d || !d.dates || d.dates.length === 0) return null;
    const first = tcgFormatDateInt(d.dates[0]);
    const last = tcgFormatDateInt(d.dates[d.dates.length - 1]);
    return { count: d.dates.length, first, last };
  });

  protected setChartType(value: string): void {
    if (value === 'candlestick' || value === 'line') {
      this.chartType.set(value);
    }
  }

  protected get downloadFilename(): string {
    return `${this._collection()}-${this._instrument()}-prices`;
  }
}
