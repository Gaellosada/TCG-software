import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnDestroy,
  Output,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { TcgChartComponent } from '../../../components/chart/tcg-chart.component';
import {
  createVerticalLineTrace,
  hiddenOverlayAxis,
} from '../../../components/chart/chart-theme';
import {
  TcgChainSnapshotResponse,
  TcgChainSnapshotPoint,
  TcgOptionsApi,
} from '../../../api/tcg-options-api.service';

type Field = 'iv' | 'delta';
type XAxisMode = 'strike' | 'K_over_S';

/**
 * Smile-snapshot panel — IV (or delta) vs strike for one expiration.
 * Mirrors React's `ChainSnapshotPanel.jsx`.
 */
@Component({
  selector: 'tcg-chain-snapshot-panel',
  standalone: true,
  imports: [CommonModule, TcgChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-chain-snapshot-panel.component.html',
  styleUrls: ['./tcg-chain-snapshot-panel.component.css'],
})
export class TcgChainSnapshotPanelComponent implements OnDestroy {
  @Input({ required: true }) set root(value: string) {
    this._root.set(value);
  }
  get root(): string {
    return this._root();
  }
  @Input({ required: true }) set date(value: string) {
    this._date.set(value);
  }
  get date(): string {
    return this._date();
  }
  @Input() set type(value: 'C' | 'P') {
    this._type.set(value);
  }
  get type(): 'C' | 'P' {
    return this._type();
  }
  @Input({ required: true }) set expiration(value: string) {
    this._expiration.set(value);
  }
  get expiration(): string {
    return this._expiration();
  }
  @Input() set expirationCycle(value: string | null) {
    this._cycle.set(value);
  }
  get expirationCycle(): string | null {
    return this._cycle();
  }

  @Output() readonly snapshotData = new EventEmitter<TcgChainSnapshotResponse>();
  @Output() readonly closed = new EventEmitter<void>();

  protected readonly _root = signal('');
  protected readonly _date = signal('');
  protected readonly _type = signal<'C' | 'P'>('C');
  protected readonly _expiration = signal('');
  protected readonly _cycle = signal<string | null>(null);

  protected readonly field = signal<Field>('iv');
  protected readonly xAxis = signal<XAxisMode>('strike');

  protected readonly data = signal<TcgChainSnapshotResponse | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<Error | null>(null);

  private readonly api = inject(TcgOptionsApi);
  private sub: Subscription | null = null;

  constructor() {
    effect(() => {
      const root = this._root();
      const date = this._date();
      const type = this._type();
      const expiration = this._expiration();
      const cycle = this._cycle();
      const field = this.field();
      if (!root || !date || !expiration) return;
      this.fetch(root, date, type, expiration, cycle, field);
    });
  }

  private fetch(
    root: string,
    date: string,
    type: 'C' | 'P',
    expiration: string,
    cycle: string | null,
    field: Field,
  ): void {
    if (this.sub) this.sub.unsubscribe();
    this.loading.set(true);
    this.error.set(null);
    this.sub = this.api
      .getChainSnapshot(root, {
        date,
        type,
        expirations: [expiration],
        field,
        ...(cycle ? { expiration_cycle: cycle } : {}),
      })
      .subscribe({
        next: (res) => {
          this.data.set(res);
          this.loading.set(false);
          this.snapshotData.emit(res);
        },
        error: (err: unknown) => {
          this.error.set(err instanceof Error ? err : new Error(String(err)));
          this.loading.set(false);
        },
      });
  }

  ngOnDestroy(): void {
    if (this.sub) this.sub.unsubscribe();
  }

  protected readonly chartTitle = computed(() => {
    const r = this._root();
    const d = this._date();
    const e = this._expiration();
    const f = this.field();
    const cyc = this._cycle();
    if (!r || !d || !e) return '';
    const fieldLabel = f === 'delta' ? 'Delta' : 'IV';
    const cycleLabel = cyc ? cyc : 'all cycles';
    return `${r} — ${d} — exp ${e} — ${cycleLabel} — ${fieldLabel}`;
  });

  protected readonly tracesAndAxis = computed<{
    traces: Array<Record<string, unknown>>;
    hasAtmLine: boolean;
  }>(() => {
    const d = this.data();
    if (!d || !Array.isArray(d.series) || d.series.length === 0) {
      return { traces: [], hasAtmLine: false };
    }
    const series = d.series[0];
    if (!series || !Array.isArray(series.points)) {
      return { traces: [], hasAtmLine: false };
    }
    const xAxis = this.xAxis();
    const field = this.field();
    const xs: Array<number | undefined> = [];
    const ys: Array<number | null> = [];
    for (const pt of series.points as TcgChainSnapshotPoint[]) {
      const x = xAxis === 'K_over_S' ? pt.K_over_S : pt.strike;
      const y =
        pt.value != null && pt.value.value !== null ? Number(pt.value.value) : null;
      xs.push(x);
      ys.push(y);
    }
    const xLabel = xAxis === 'K_over_S' ? 'K/S' : 'Strike';
    const yLabel = field === 'delta' ? 'Delta' : 'IV';
    const traceList: Array<Record<string, unknown>> = [
      {
        x: xs,
        y: ys,
        type: 'scatter',
        mode: 'lines+markers',
        name: yLabel,
        connectgaps: false,
        line: { width: 1.5, color: 'rgba(14, 165, 233, 0.5)' },
        marker: { size: 5, color: '#0ea5e9' },
        hovertemplate: `${xLabel}: %{x}<br>${yLabel}: %{y:.4f}<extra></extra>`,
      },
    ];

    const underlyingValue =
      d.underlying_price && d.underlying_price.value != null
        ? Number(d.underlying_price.value)
        : null;
    let atmX: number | null = null;
    if (xAxis === 'K_over_S') atmX = 1;
    else if (underlyingValue != null) atmX = underlyingValue;
    let atmAdded = false;
    if (atmX != null) {
      const atmLabel =
        xAxis === 'K_over_S' ? 'ATM (K = S)' : `ATM (S = ${atmX.toFixed(2)})`;
      traceList.push(
        createVerticalLineTrace([String(atmX)], {
          name: atmLabel,
          color: '#f59e0b',
          dash: 'dash',
          yaxisKey: 'y2',
        }),
      );
      atmAdded = true;
    }
    return { traces: traceList, hasAtmLine: atmAdded };
  });

  protected readonly traces = computed(() => this.tracesAndAxis().traces);

  protected readonly layoutOverrides = computed<Record<string, unknown>>(() => {
    const { hasAtmLine } = this.tracesAndAxis();
    const xAxis = this.xAxis();
    const field = this.field();
    return {
      title: { text: this.chartTitle(), font: { size: 13 } },
      xaxis: {
        title: { text: xAxis === 'K_over_S' ? 'K / S' : 'Strike', font: { size: 11 } },
        type: 'linear',
      },
      yaxis: { title: { text: field === 'delta' ? 'Delta' : 'IV', font: { size: 11 } } },
      ...(hasAtmLine ? { yaxis2: hiddenOverlayAxis() } : {}),
    };
  });

  protected setField(value: Field): void {
    this.field.set(value);
  }
  protected setXAxis(value: XAxisMode): void {
    this.xAxis.set(value);
  }

  protected get downloadFilename(): string {
    return `${this._root()}-${this._date()}-${this._expiration()}-${this.field()}`;
  }
}
