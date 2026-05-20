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
import { TRACE_COLORS } from '../../../components/chart/chart-theme';
import { TcgChartMarker } from '../../../components/chart/chart-markers';
import { TcgOptionsApi, TcgOptionRootInfo } from '../../../api/tcg-options-api.service';
import { tcgAddDays, tcgTodayIso } from '../data-format';

interface ResolveResult {
  dates: string[];
  streams: Record<string, { values: number[]; diagnostics?: Array<string | null> }>;
  rolls?: Record<
    string,
    Array<{
      date: string;
      sold?: { value?: number | null } & Record<string, unknown>;
      bought?: { value?: number | null } & Record<string, unknown>;
    }>
  >;
}

/**
 * Continuous-options materialised chart. Port of React's
 * `ContinuousOptionsChart.jsx`.
 *
 * SCOPE NOTE (PROBLEMS): the React component uses a full `OptionStreamForm`
 * with 8 maturity kinds × 3 selection kinds × 8 streams plus an
 * `OptionDateRangeControl` with preset buttons. Phase A deferred the deep
 * form subsystem. This Angular port currently surfaces a *minimal* control
 * strip — root dropdown + start/end date inputs + Resolve button — that
 * builds a sensible default `OptionStreamRef` (call, by_delta=0.25,
 * next_third_friday, mid stream) so the dev-harness can render charts.
 *
 * The full UI lands in the wave that ports Indicators / Signals (where
 * OptionStreamForm is also required).
 */
@Component({
  selector: 'tcg-continuous-options-chart',
  standalone: true,
  imports: [CommonModule, TcgChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-continuous-options-chart.component.html',
  styleUrls: ['../_chart-base.css', './tcg-continuous-options-chart.component.css'],
})
export class TcgContinuousOptionsChartComponent {
  @Input({ required: true }) set collection(value: string) {
    this._collection.set(value);
    // Default the stream root to the page's collection.
    if (this.streamCollection() !== value) this.streamCollection.set(value);
  }
  get collection(): string {
    return this._collection();
  }

  private readonly optionsApi = inject(TcgOptionsApi);

  protected readonly _collection = signal('');
  protected readonly streamCollection = signal('');
  protected readonly availableRoots = signal<TcgOptionRootInfo[]>([]);
  protected readonly rootsLoading = signal(false);
  protected readonly rootsError = signal<Error | null>(null);

  protected readonly start = signal<string>(tcgAddDays(tcgTodayIso(), -365));
  protected readonly end = signal<string>(tcgTodayIso());

  protected readonly result = signal<ResolveResult | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<Error | null>(null);

  constructor() {
    effect(() => {
      this.rootsLoading.set(true);
      this.rootsError.set(null);
      firstValueFrom(this.optionsApi.getOptionRoots())
        .then((r) => this.availableRoots.set(r.roots ?? []))
        .catch((err: unknown) =>
          this.rootsError.set(err instanceof Error ? err : new Error(String(err))),
        )
        .finally(() => this.rootsLoading.set(false));
    });

    // Re-anchor the stream root when the underlying collection changes.
    effect(() => {
      const c = this._collection();
      const roots = this.availableRoots();
      if (!c || roots.length === 0) return;
      const match = roots.find(
        (r) =>
          String((r as Record<string, unknown>)['collection'] ?? r.name) === c,
      );
      if (match) {
        this.streamCollection.set(
          String((match as Record<string, unknown>)['collection'] ?? match.name),
        );
      }
    });
  }

  protected setStreamCollection(value: string): void {
    this.streamCollection.set(value);
  }

  protected setStart(value: string): void {
    this.start.set(value);
  }
  protected setEnd(value: string): void {
    this.end.set(value);
  }

  /** Build a sensible default OptionStreamRef. See SCOPE NOTE above. */
  private buildDefaultStreamRef(): Record<string, unknown> {
    return {
      collection: this.streamCollection(),
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'next_third_friday' },
      selection: { kind: 'by_delta', target: 0.25 },
      stream: 'mid',
    };
  }

  async resolve(): Promise<void> {
    if (!this.streamCollection()) return;
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);
    const taskId =
      typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      const ref = this.buildDefaultStreamRef();
      const label = 'MID / Call / by delta';
      const res = (await firstValueFrom(
        this.optionsApi.resolveOptionStream(
          [{ ref, label }],
          this.start(),
          this.end(),
          taskId,
        ),
      )) as ResolveResult;
      this.result.set(res);
    } catch (err: unknown) {
      this.error.set(err instanceof Error ? err : new Error(String(err)));
    } finally {
      this.loading.set(false);
    }
  }

  protected readonly traces = computed<Array<Record<string, unknown>>>(() => {
    const r = this.result();
    if (!r || !r.dates || r.dates.length === 0) return [];
    const t: Array<Record<string, unknown>> = [];
    let colorIdx = 0;
    for (const [label, stream] of Object.entries(r.streams)) {
      t.push({
        x: r.dates,
        y: stream.values,
        type: 'scatter',
        mode: 'lines',
        name: label,
        line: { color: TRACE_COLORS[colorIdx % TRACE_COLORS.length], width: 1 },
        hovertemplate: '%{x}<br>' + label + ': %{y:,.4f}<extra></extra>',
        connectgaps: false,
      });
      colorIdx++;
    }
    return t;
  });

  protected readonly markers = computed<TcgChartMarker[]>(() => {
    const out: TcgChartMarker[] = [];
    const rolls = this.result()?.rolls;
    if (!rolls) return out;
    for (const label of Object.keys(rolls)) {
      const events = rolls[label];
      if (!Array.isArray(events)) continue;
      for (const roll of events) {
        if (roll?.sold?.value != null) {
          out.push({ x: roll.date, y: Number(roll.sold.value), kind: 'sell', tooltip: roll.sold });
        }
        if (roll?.bought?.value != null) {
          out.push({ x: roll.date, y: Number(roll.bought.value), kind: 'buy', tooltip: roll.bought });
        }
      }
    }
    return out;
  });

  protected readonly displayMeta = computed(() => {
    const r = this.result();
    if (!r || !r.dates || r.dates.length === 0) return null;
    return {
      count: r.dates.length,
      first: r.dates[0],
      last: r.dates[r.dates.length - 1],
    };
  });

  protected get downloadFilename(): string {
    return `${this._collection()}-continuous-options`;
  }

  protected rootValue(r: TcgOptionRootInfo): string {
    return String((r as Record<string, unknown>)['collection'] ?? r.name);
  }
}
