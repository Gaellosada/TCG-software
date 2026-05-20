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
import { TcgChartMarker } from '../../../components/chart/chart-markers';
import { TcgDataApi } from '../../../api/tcg-data-api.service';
import { TcgUserSettingsService } from '../../../services/tcg-user-settings.service';
import { tcgFormatDateInt, tcgPrepareChartData, TcgRawPriceData } from '../data-format';

interface ContinuousResponse extends TcgRawPriceData {
  roll_dates?: number[];
  contracts?: string[];
}

/**
 * Continuous-futures chart with adjustment / cycle / roll-offset controls
 * and roll markers. Port of React's `pages/Data/ContinuousChart.jsx`.
 */
@Component({
  selector: 'tcg-continuous-chart',
  standalone: true,
  imports: [CommonModule, TcgChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-continuous-chart.component.html',
  styleUrls: ['../_chart-base.css', './tcg-continuous-chart.component.css'],
})
export class TcgContinuousChartComponent {
  @Input({ required: true }) set collection(value: string) {
    this._collection.set(value);
    this.cycle.set('');
  }
  get collection(): string {
    return this._collection();
  }

  private readonly dataApi = inject(TcgDataApi);
  private readonly userSettings = inject(TcgUserSettingsService, { optional: true });

  protected readonly _collection = signal('');
  protected readonly adjustment = signal<'none' | 'ratio' | 'difference'>('none');
  protected readonly cycle = signal('');
  protected readonly rollOffset = signal(2);
  protected readonly chartType = signal<'line' | 'candlestick'>(
    this.userSettings?.chartType() ?? 'line',
  );

  protected readonly data = signal<ContinuousResponse | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<Error | null>(null);
  protected readonly availableCycles = signal<string[]>([]);

  constructor() {
    effect(() => {
      const pref = this.userSettings?.chartType();
      if (pref) this.chartType.set(pref);
    });

    // Cycles fetch.
    effect(() => {
      const coll = this._collection();
      if (!coll) return;
      firstValueFrom(this.dataApi.getAvailableCycles(coll))
        .then((cs) => this.availableCycles.set(cs))
        .catch(() => this.availableCycles.set([]));
    });

    // Continuous series fetch on every input change.
    effect(() => {
      const coll = this._collection();
      const adj = this.adjustment();
      const cyc = this.cycle();
      const off = this.rollOffset();
      if (!coll) return;
      this.fetchSeries(coll, adj, cyc, off);
    });
  }

  private async fetchSeries(
    collection: string,
    adjustment: string,
    cycle: string,
    rollOffset: number,
  ): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const res = (await firstValueFrom(
        this.dataApi.getContinuousSeries(collection, {
          strategy: 'front_month',
          adjustment,
          cycle: cycle || undefined,
          rollOffset,
        }),
      )) as ContinuousResponse;
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

  protected readonly rollDates = computed<number[]>(() => this.data()?.roll_dates ?? []);

  /** Roll markers — see React's ContinuousChart for the derivation. */
  protected readonly markers = computed<TcgChartMarker[]>(() => {
    const d = this.data();
    if (!d || !d.roll_dates || d.roll_dates.length === 0) return [];
    const rolls = d.roll_dates;
    const contracts = d.contracts ?? [];
    const dates = d.dates ?? [];
    const close = d.close ?? [];
    const out: TcgChartMarker[] = [];
    for (let k = 0; k < rolls.length; k++) {
      const rollDateInt = rolls[k];
      const i = dates.indexOf(rollDateInt);
      if (i <= 0) continue;
      const sellPrice = close[i - 1];
      const buyPrice = close[i];
      if (!Number.isFinite(sellPrice) || !Number.isFinite(buyPrice)) continue;
      const oldContract = contracts[k];
      const newContract = contracts[k + 1];
      const xLabel = tcgFormatDateInt(rollDateInt);
      out.push({
        x: xLabel,
        y: sellPrice,
        kind: 'sell',
        customdata: [oldContract, sellPrice],
      });
      out.push({
        x: xLabel,
        y: buyPrice,
        kind: 'buy',
        customdata: [newContract, buyPrice],
      });
    }
    return out;
  });

  protected readonly markerHovertemplates = {
    sell: '<b>Sell</b><br>%{customdata[0]}<br>Close: %{customdata[1]:,.2f}<extra></extra>',
    buy: '<b>Buy</b><br>%{customdata[0]}<br>Close: %{customdata[1]:,.2f}<extra></extra>',
  };

  protected readonly traces = computed<Array<Record<string, unknown>>>(() => {
    const data = this.data();
    const prepared = this.prepared();
    if (!data || !prepared || !data.dates) return [];
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
              title: { text: 'Volume', font: { size: 11, color: colors.secondaryFont } },
              anchor: 'x',
            },
          }
        : {}),
    };
  });

  protected readonly displayMeta = computed(() => {
    const d = this.data();
    if (!d || !d.dates || d.dates.length === 0) return null;
    const rolls = this.rollDates().length;
    const contractsLen = d.contracts?.length ?? 0;
    return {
      count: d.dates.length,
      first: tcgFormatDateInt(d.dates[0]),
      last: tcgFormatDateInt(d.dates[d.dates.length - 1]),
      rolls,
      contracts: contractsLen,
    };
  });

  protected readonly adjustmentOptions = [
    { value: 'none', label: 'None' },
    { value: 'ratio', label: 'Ratio' },
    { value: 'difference', label: 'Difference' },
  ];

  protected setChartType(value: string): void {
    if (value === 'candlestick' || value === 'line') this.chartType.set(value);
  }
  protected setAdjustment(value: string): void {
    if (value === 'none' || value === 'ratio' || value === 'difference') {
      this.adjustment.set(value);
    }
  }
  protected setCycle(value: string): void {
    this.cycle.set(value);
  }
  protected setRollOffset(value: string | number): void {
    const n = typeof value === 'string' ? parseInt(value, 10) : value;
    this.rollOffset.set(Math.max(0, Math.min(30, Number.isFinite(n) ? n : 0)));
  }

  protected get downloadFilename(): string {
    return `${this._collection()}-continuous-${this.adjustment()}${this.cycle() ? `-${this.cycle()}` : ''}`;
  }
}
