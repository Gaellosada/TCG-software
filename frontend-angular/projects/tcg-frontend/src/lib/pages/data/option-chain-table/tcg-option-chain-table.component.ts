import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Injector,
  Input,
  OnChanges,
  OnInit,
  Output,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import {
  TcgChainResponse,
  TcgChainRow,
  TcgComputeResult,
  TcgOptionsApi,
} from '../../../api/tcg-options-api.service';
import { TcgContractRef } from '../data-types';
import { tcgFmt, tcgFmtInt } from '../data-format';
import {
  tcgChainRows,
  tcgUseOptionsChain,
} from '../options-chain.signal';
import {
  tcgReversedExpirations,
  tcgUseOptionExpirations,
} from '../option-expirations.signal';

interface MergedRow {
  expiration: string;
  expiration_cycle: string;
  strike: number;
  call: TcgChainRow | null;
  put: TcgChainRow | null;
}

/**
 * Long-form option chain table with filter strip, ComputeResult-aware
 * cell rendering, and click-to-select rows. Mirrors React's
 * `pages/Data/OptionChainTable.jsx`.
 *
 * Performance: OnPush change detection + row trackBy. The React side
 * regularly carries 1000+ rows on OPT_SP_500 — keep additional change-
 * detection sources minimal.
 */
@Component({
  selector: 'tcg-option-chain-table',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-option-chain-table.component.html',
  styleUrls: ['./tcg-option-chain-table.component.css'],
})
export class TcgOptionChainTableComponent implements OnInit, OnChanges {
  @Input({ required: true }) root!: string;
  @Input() initialFilters?: {
    date?: string | null;
    expirationMin?: string | null;
  };
  @Input() selectedContract: TcgContractRef | null = null;

  @Output() readonly rowClick = new EventEmitter<TcgContractRef>();

  private readonly optionsApi = inject(TcgOptionsApi);
  private readonly injector = inject(Injector);

  private readonly rootSig = signal<string | null>(null);
  private readonly expirationsResource = tcgUseOptionExpirations(this.rootSig, this.optionsApi);
  protected readonly expirations = this.expirationsResource.expirations;
  protected readonly expirationsLoading = this.expirationsResource.loading;
  protected readonly expirationOptions = tcgReversedExpirations(this.expirations);

  // Initialized with null root; updated on `ngOnInit` once `@Input`s are set.
  private readonly chain = tcgUseOptionsChain(this.optionsApi, null, {});
  protected readonly filters = this.chain.filters;
  protected readonly chainData = this.chain.chainData;
  protected readonly loading = this.chain.loading;

  protected readonly rows = tcgChainRows(this.chainData);
  protected readonly chainError = computed(() => {
    const d = this.chainData();
    if (!d) return null;
    if ('error' in d && d.error) return d.error as Error;
    return null;
  });

  protected readonly cycleOptions = computed<string[]>(() => {
    const r = this.rows() as TcgChainRow[] | null;
    if (!r) return [];
    const set = new Set<string>();
    for (const row of r) {
      const cyc = (row.expiration_cycle ?? '').trim();
      if (cyc) set.add(cyc);
    }
    return [...set].sort();
  });

  protected readonly merged = computed<MergedRow[]>(() => {
    const r = this.rows() as TcgChainRow[] | null;
    if (!r || r.length === 0) return [];
    const map = new Map<string, MergedRow>();
    for (const row of r) {
      const key = `${row.expiration}|${row.strike}`;
      let entry = map.get(key);
      if (!entry) {
        entry = {
          expiration: row.expiration,
          expiration_cycle: row.expiration_cycle ?? '',
          strike: row.strike,
          call: null,
          put: null,
        };
        map.set(key, entry);
      }
      if (row.type === 'C') entry.call = row;
      else if (row.type === 'P') entry.put = row;
    }
    return [...map.values()].sort((a, b) => {
      if (a.expiration !== b.expiration) return a.expiration < b.expiration ? 1 : -1;
      return b.strike - a.strike;
    });
  });

  protected readonly chainMeta = computed(() => {
    const d = this.chainData();
    if (!d || ('error' in d && d.error)) return null;
    const resp = d as TcgChainResponse;
    const underlying =
      resp.underlying_price && resp.underlying_price.value != null
        ? Number(resp.underlying_price.value).toFixed(2)
        : null;
    return {
      count: this.rows()?.length ?? 0,
      date: resp.date ?? null,
      underlying,
    };
  });

  protected readonly selectedId = computed<string | null>(() => {
    const sc = this.selectedContractSig();
    if (!sc) return null;
    if (sc.collection !== this.root) return null;
    return sc.instrument_id ?? null;
  });
  private readonly selectedContractSig = signal<TcgContractRef | null>(null);

  // Debounce timer handle so filter changes coalesce 200ms.
  private debounceHandle: ReturnType<typeof setTimeout> | null = null;

  ngOnChanges(): void {
    if (this.root !== this.rootSig()) {
      this.rootSig.set(this.root);
      this.chain.updateFilters({ root: this.root });
    }
    this.selectedContractSig.set(this.selectedContract);
  }

  ngOnInit(): void {
    this.rootSig.set(this.root);
    this.chain.updateFilters({
      root: this.root,
      date: this.initialFilters?.date ?? this.chain.filters().date,
      expirationMin:
        this.initialFilters?.expirationMin ?? this.chain.filters().expirationMin,
    });
    this.selectedContractSig.set(this.selectedContract);

    runInInjectionContext(this.injector, () => {
      // Snap min/max to a valid expiration once the list loads.
      effect(() => {
        const exps = this.expirations();
        if (!exps || exps.length === 0) return;
        const latest = exps[exps.length - 1];
        const f = this.filters();
        const updates: Partial<typeof f> = {};
        if (!f.expirationMin || !exps.includes(f.expirationMin)) {
          updates.expirationMin = latest;
        }
        if (!f.expirationMax || !exps.includes(f.expirationMax)) {
          updates.expirationMax = latest;
        }
        if (Object.keys(updates).length > 0) this.chain.updateFilters(updates);
      });

      // Auto-fetch on filter changes (200 ms debounce).
      effect(() => {
        const f = this.filters();
        if (!f.root || !f.date) return;
        if (this.debounceHandle) clearTimeout(this.debounceHandle);
        this.debounceHandle = setTimeout(() => {
          void this.chain.fetchChain();
        }, 200);
      });
    });
  }

  // -------------------------------------------------------------------
  // Filter setters
  // -------------------------------------------------------------------
  protected setDate(value: string): void {
    this.chain.updateFilters({ date: value || null });
  }
  protected setExpirationMin(value: string): void {
    this.chain.updateFilters({ expirationMin: value || null });
  }
  protected setExpirationMax(value: string): void {
    this.chain.updateFilters({ expirationMax: value || null });
  }
  protected setStrikeMin(value: string): void {
    this.chain.updateFilters({ strikeMin: value === '' ? null : Number(value) });
  }
  protected setStrikeMax(value: string): void {
    this.chain.updateFilters({ strikeMax: value === '' ? null : Number(value) });
  }
  protected setExpirationCycle(value: string): void {
    this.chain.updateFilters({ expirationCycle: value === '' ? null : value });
  }
  protected setComputeMissing(checked: boolean): void {
    this.chain.updateFilters({ computeMissing: checked });
  }

  protected refresh(): void {
    void this.chain.fetchChain();
  }

  // -------------------------------------------------------------------
  // Row click
  // -------------------------------------------------------------------
  protected handleRowClick(entry: MergedRow, event: MouseEvent): void {
    const td = (event.target as HTMLElement | null)?.closest('td');
    const side = td?.getAttribute('data-side');
    const target =
      side === 'call'
        ? entry.call
        : side === 'put'
          ? entry.put
          : entry.call ?? entry.put;
    if (!target) return;
    this.rowClick.emit({
      collection: this.root,
      instrument_id: target.contract_id,
      expiry: target.expiration,
      strike: target.strike,
      optionType: target.type,
    });
  }

  protected selectedSideFor(entry: MergedRow): 'call' | 'put' | null {
    const id = this.selectedId();
    if (!id) return null;
    if (entry.call?.contract_id === id) return 'call';
    if (entry.put?.contract_id === id) return 'put';
    return null;
  }

  // -------------------------------------------------------------------
  // Cell formatters
  // -------------------------------------------------------------------
  protected fmt(v: unknown, decimals: number): string {
    if (typeof v === 'number') return tcgFmt(v, decimals);
    return tcgFmt(v as number | null | undefined, decimals);
  }
  protected fmtInt(v: unknown): string {
    return tcgFmtInt(v as number | null | undefined);
  }

  protected resultKind(r: TcgComputeResult | undefined | null): 'stored' | 'computed' | 'missing' {
    if (!r) return 'missing';
    if (r.source === 'computed') return 'computed';
    if (r.source === 'missing' || r.value == null) return 'missing';
    return 'stored';
  }

  protected computedTooltip(r: TcgComputeResult): string {
    const parts: string[] = [];
    if (r.model) parts.push(`Computed via ${r.model}.`);
    const inputs = r.inputs_used ?? {};
    const inputBits: string[] = [];
    if (inputs.underlying_price != null) inputBits.push(`F = ${inputs.underlying_price}`);
    if (inputs.iv != null) inputBits.push(`IV = ${inputs.iv}`);
    if (inputs.ttm != null) inputBits.push(`T = ${inputs.ttm} yr`);
    if (inputs.r != null) inputBits.push(`r = ${inputs.r}`);
    if (inputBits.length > 0) parts.push(`Inputs: ${inputBits.join(', ')}.`);
    return parts.join(' ');
  }

  protected missingTooltip(r: TcgComputeResult): string {
    if (r.error_code) return `${r.error_code}: ${r.error_detail ?? ''}`;
    return 'Missing';
  }

  protected expChanged(idx: number): boolean {
    const m = this.merged();
    if (idx === 0) return false;
    return m[idx - 1].expiration !== m[idx].expiration;
  }

  protected trackRow(_idx: number, entry: MergedRow): string {
    return `${entry.expiration}|${entry.strike}`;
  }
}
