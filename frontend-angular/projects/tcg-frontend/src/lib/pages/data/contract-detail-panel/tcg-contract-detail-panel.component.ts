import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
  computed,
  inject,
  signal,
} from '@angular/core';
import { TcgChartComponent } from '../../../components/chart/tcg-chart.component';
import {
  TRACE_COLORS,
  createVerticalLineTrace,
  hiddenOverlayAxis,
} from '../../../components/chart/chart-theme';
import {
  TcgComputeResult,
  TcgContractRow,
  TcgOptionsApi,
} from '../../../api/tcg-options-api.service';
import { tcgDaysBetween, tcgTodayIso } from '../data-format';
import { tcgUseContractSeries } from '../contract-series.signal';

const AMERICAN_EXERCISE_ROOTS = new Set(['OPT_T_NOTE_10_Y', 'OPT_T_BOND']);

const GREEK_DEFS = [
  { key: 'iv', label: 'IV', color: TRACE_COLORS[1] },
  { key: 'delta', label: 'Δ', color: TRACE_COLORS[2] },
  { key: 'gamma', label: 'Γ', color: TRACE_COLORS[3] },
  { key: 'theta', label: 'Θ', color: TRACE_COLORS[4] },
  { key: 'vega', label: 'ν', color: TRACE_COLORS[5] },
] as const;

const LIFECYCLE_MARKERS = [
  { key: 'firstTrade', label: 'First trade', color: '#94a3b8', dash: 'dot' },
  { key: 'expiration', label: 'Expiration', color: '#ef4444', dash: 'dash' },
  { key: 'atmCross', label: 'ATM cross', color: '#f59e0b', dash: 'dot' },
  { key: 'delta30', label: '|Δ|=0.30', color: '#10b981', dash: 'dot' },
  { key: 'delta50', label: '|Δ|=0.50', color: '#6366f1', dash: 'dot' },
  { key: 'delta70', label: '|Δ|=0.70', color: '#ec4899', dash: 'dot' },
] as const;

type GreekKey = (typeof GREEK_DEFS)[number]['key'];

/**
 * Per-contract detail panel — Mid + Volume chart, optional Greek
 * overlays, life-cycle markers, contract metadata sidebar. Mirrors React's
 * `ContractDetailPanel.jsx`.
 */
@Component({
  selector: 'tcg-contract-detail-panel',
  standalone: true,
  imports: [CommonModule, TcgChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-contract-detail-panel.component.html',
  styleUrls: ['./tcg-contract-detail-panel.component.css'],
})
export class TcgContractDetailPanelComponent {
  @Input({ required: true }) set collection(value: string) {
    this._collection.set(value);
  }
  get collection(): string {
    return this._collection();
  }

  @Input({ required: true }) set instrumentId(value: string) {
    this._instrumentId.set(value);
  }
  get instrumentId(): string {
    return this._instrumentId();
  }

  @Output() readonly closed = new EventEmitter<void>();

  protected readonly _collection = signal('');
  protected readonly _instrumentId = signal('');

  protected readonly computeMissing = signal(true);
  protected readonly overlayState = signal<Record<GreekKey, boolean>>({
    iv: false,
    delta: false,
    gamma: false,
    theta: false,
    vega: false,
  });
  protected readonly showMarkers = signal(false);

  private readonly api = inject(TcgOptionsApi);
  private readonly resource = tcgUseContractSeries(
    {
      collection: this._collection,
      contractId: this._instrumentId,
      computeMissing: this.computeMissing,
    },
    this.api,
  );
  protected readonly loading = this.resource.loading;
  protected readonly error = this.resource.error;
  protected readonly data = this.resource.data;

  protected readonly dataIsStale = computed(() => {
    const d = this.data();
    return !!(d && d.contract && d.contract.contract_id !== this._instrumentId());
  });

  protected readonly contract = computed(() => {
    if (this.dataIsStale()) return null;
    return this.data()?.contract ?? null;
  });

  protected readonly rows = computed<TcgContractRow[]>(() => {
    if (this.dataIsStale()) return [];
    const d = this.data();
    return Array.isArray(d?.rows) ? d!.rows : [];
  });

  protected readonly showLoading = computed(() => this.loading() || this.dataIsStale());

  protected readonly dte = computed<number | null>(() => {
    const c = this.contract();
    if (!c?.expiration) return null;
    return tcgDaysBetween(c.expiration, tcgTodayIso());
  });

  protected readonly showAmericanNote = computed(() => {
    const c = this.contract();
    if (!c) return false;
    return AMERICAN_EXERCISE_ROOTS.has(String(c.root_underlying ?? this._collection()));
  });

  protected readonly dataRange = computed(() => {
    const r = this.rows();
    if (r.length === 0) return '—';
    return `${r[0].date} → ${r[r.length - 1].date}`;
  });

  protected readonly traces = computed<Array<Record<string, unknown>>>(() => {
    const rows = this.rows();
    if (rows.length === 0) return [];
    const dates = rows.map((r) => r.date);
    const out: Array<Record<string, unknown>> = [];

    out.push({
      x: dates,
      y: rows.map((r) => (r.mid == null ? null : Number(r.mid))),
      type: 'scatter',
      mode: 'lines',
      name: 'Mid',
      line: { color: TRACE_COLORS[0], width: 1.5 },
      hovertemplate: '%{x}<br>Mid: %{y:,.4f}<extra></extra>',
    });

    out.push({
      x: dates,
      y: rows.map((r) => (r.volume == null ? null : Number(r.volume))),
      type: 'bar',
      name: 'Volume',
      yaxis: 'y2',
      marker: { color: 'rgba(14, 165, 233, 0.3)' },
      hovertemplate: '%{x}<br>Volume: %{y:,.0f}<extra></extra>',
    });

    let hasGreek = false;
    const overlays = this.overlayState();
    for (const g of GREEK_DEFS) {
      if (!overlays[g.key]) continue;
      const { xs, ys, anyComputed, anyValue } = this.extractGreekSeries(rows, g.key);
      if (!anyValue) continue;
      hasGreek = true;
      out.push({
        x: xs,
        y: ys,
        type: 'scatter',
        mode: 'lines',
        name: g.label,
        yaxis: 'y3',
        line: {
          color: g.color,
          width: 1,
          dash: anyComputed ? 'dash' : 'solid',
        },
        connectgaps: false,
        hovertemplate: `%{x}<br>${g.label}: %{y:.4f}<extra></extra>`,
      });
    }

    if (this.showMarkers()) {
      const markerDates = this.computeLifecycleDates(rows);
      for (const m of LIFECYCLE_MARKERS) {
        const d = markerDates[m.key];
        if (!d) continue;
        out.push(
          createVerticalLineTrace([d], {
            name: m.label,
            color: m.color,
            dash: m.dash,
            yaxisKey: 'y4',
          }),
        );
      }
    }

    // Stash the secondary axes on the host computed so layoutOverrides
    // can read whether we need y3/y4.
    this._hasGreekOverlay = hasGreek;
    return out;
  });

  // Internal tracking — read by `layoutOverrides`.
  private _hasGreekOverlay = false;

  protected readonly layoutOverrides = computed<Record<string, unknown>>(() => {
    // Touch traces() so the hasGreek tracking is fresh.
    this.traces();
    const hasMarkers =
      this.showMarkers() &&
      Object.values(this.computeLifecycleDates(this.rows())).some((v) => !!v);
    return {
      xaxis: { anchor: 'y2' },
      yaxis: { title: { text: 'Mid', font: { size: 11 } }, domain: [0.28, 1.0] },
      yaxis2: {
        domain: [0, 0.2],
        zeroline: false,
        showgrid: true,
        title: { text: 'Volume', font: { size: 11 } },
        anchor: 'x',
      },
      ...(this._hasGreekOverlay
        ? {
            yaxis3: {
              overlaying: 'y',
              side: 'right',
              showgrid: false,
              title: { text: 'Greek', font: { size: 11 } },
            },
          }
        : {}),
      ...(hasMarkers ? { yaxis4: hiddenOverlayAxis() } : {}),
    };
  });

  protected readonly greekDefs = GREEK_DEFS.map((g) => ({ key: g.key, label: g.label }));

  protected toggleOverlay(key: GreekKey, checked: boolean): void {
    this.overlayState.update((prev) => ({ ...prev, [key]: checked }));
  }
  protected toggleMarkers(checked: boolean): void {
    this.showMarkers.set(checked);
  }
  protected toggleComputeMissing(checked: boolean): void {
    this.computeMissing.set(checked);
  }

  protected emitClose(): void {
    this.closed.emit();
  }

  protected get downloadFilename(): string {
    return `${this._collection()}-${this._instrumentId()}`;
  }

  // -------------------------------------------------------------------
  // Helpers — same semantics as React's ContractDetailPanel.
  // -------------------------------------------------------------------
  private extractGreekSeries(
    rows: TcgContractRow[],
    key: GreekKey,
  ): {
    xs: Array<string | undefined>;
    ys: Array<number | null>;
    anyComputed: boolean;
    anyValue: boolean;
  } {
    const xs: Array<string | undefined> = [];
    const ys: Array<number | null> = [];
    let anyComputed = false;
    let anyValue = false;
    for (const row of rows) {
      const cr = (row as Record<string, unknown>)[key] as TcgComputeResult | undefined;
      if (!cr) {
        xs.push(row?.date);
        ys.push(null);
        continue;
      }
      if (cr.source === 'computed') anyComputed = true;
      const v =
        cr.source === 'missing' || cr.value == null ? null : Number(cr.value);
      if (v !== null) anyValue = true;
      xs.push(row.date);
      ys.push(v);
    }
    return { xs, ys, anyComputed, anyValue };
  }

  private computeLifecycleDates(rows: TcgContractRow[]): {
    firstTrade: string | null;
    expiration: string | null;
    atmCross: string | null;
    delta30: string | null;
    delta50: string | null;
    delta70: string | null;
    [key: string]: string | null;
  } {
    const result = {
      firstTrade: null as string | null,
      expiration: null as string | null,
      atmCross: null as string | null,
      delta30: null as string | null,
      delta50: null as string | null,
      delta70: null as string | null,
    };
    if (!rows || rows.length === 0) return result;
    result.firstTrade = rows[0].date ?? null;
    const c = this.contract();
    if (c?.expiration) result.expiration = c.expiration as string;

    const strike = c?.strike;
    if (strike != null) {
      let bestDist = Infinity;
      let bestDate: string | null = null;
      for (const row of rows) {
        const s = row.underlying_price_stored;
        if (s == null) continue;
        const dist = Math.abs(Number(strike) - Number(s));
        if (dist < bestDist) {
          bestDist = dist;
          bestDate = row.date;
        }
      }
      result.atmCross = bestDate;
    }

    const thresholds: Array<{ key: string; level: number }> = [
      { key: 'delta30', level: 0.3 },
      { key: 'delta50', level: 0.5 },
      { key: 'delta70', level: 0.7 },
    ];
    for (const row of rows) {
      let absDelta: number | null = null;
      if (row.delta_stored != null) absDelta = Math.abs(Number(row.delta_stored));
      else if (
        row.delta &&
        row.delta.source !== 'missing' &&
        row.delta.value != null
      ) {
        absDelta = Math.abs(Number(row.delta.value));
      }
      if (absDelta == null) continue;
      for (const { key, level } of thresholds) {
        const r = result as Record<string, string | null>;
        if (r[key] === null && absDelta >= level) r[key] = row.date;
      }
      if (
        result['delta30'] !== null &&
        result['delta50'] !== null &&
        result['delta70'] !== null
      ) {
        break;
      }
    }
    return result;
  }

  protected fmt2(v: unknown): string {
    if (v == null) return '—';
    return Number(v).toFixed(2);
  }
}
