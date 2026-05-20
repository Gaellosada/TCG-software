import { CommonModule } from '@angular/common';
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  ViewChild,
  afterNextRender,
  computed,
  effect,
  inject,
  signal,
  Injector,
} from '@angular/core';
import { TcgCategoryBrowserComponent } from './category-browser/tcg-category-browser.component';
import { TcgPriceChartComponent } from './price-chart/tcg-price-chart.component';
import { TcgContinuousChartComponent } from './continuous-chart/tcg-continuous-chart.component';
import { TcgContinuousOptionsChartComponent } from './continuous-options-chart/tcg-continuous-options-chart.component';
import { TcgOptionChainTableComponent } from './option-chain-table/tcg-option-chain-table.component';
import { TcgContractDetailPanelComponent } from './contract-detail-panel/tcg-contract-detail-panel.component';
import { TcgChainSnapshotPanelComponent } from './chain-snapshot-panel/tcg-chain-snapshot-panel.component';
import { TcgOptionsApi } from '../../api/tcg-options-api.service';
import { TcgChainSnapshotResponse } from '../../api/tcg-options-api.service';
import {
  TcgContractRef,
  TcgDataSelection,
  TcgOptionsViewTab,
  tcgIsContinuousSelection,
  tcgIsOptionSelection,
} from './data-types';
import {
  tcgReversedExpirations,
  tcgUseOptionExpirations,
} from './option-expirations.signal';
import { tcgTodayIso } from './data-format';

const OPTIONS_TABS: Array<{ key: TcgOptionsViewTab; label: string }> = [
  { key: 'chain', label: 'Contracts' },
  { key: 'continuous', label: 'Continuous' },
  { key: 'snapshot', label: 'Smile' },
];

/**
 * Top-level Data page. Mirrors React's `pages/Data/DataPage.jsx`:
 *   - left rail: TcgCategoryBrowser (instrument tree);
 *   - right rail: branches on selected.type — `option` shows tabs
 *     (chain / continuous / snapshot); `continuous` shows
 *     TcgContinuousChart; default shows TcgPriceChart.
 *
 * G3/G4/G8: standalone, `tcg-` selector, `TcgDataPageComponent` class.
 */
@Component({
  selector: 'tcg-data-page',
  standalone: true,
  imports: [
    CommonModule,
    TcgCategoryBrowserComponent,
    TcgPriceChartComponent,
    TcgContinuousChartComponent,
    TcgContinuousOptionsChartComponent,
    TcgOptionChainTableComponent,
    TcgContractDetailPanelComponent,
    TcgChainSnapshotPanelComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-data-page.component.html',
  styleUrls: ['./tcg-data-page.component.css'],
})
export class TcgDataPageComponent implements AfterViewInit {
  protected readonly selected = signal<TcgDataSelection | null>(null);
  protected readonly selectedContract = signal<TcgContractRef | null>(null);

  protected readonly optionsView = signal<TcgOptionsViewTab>('chain');
  protected readonly optionsDate = signal<string>(tcgTodayIso());
  protected readonly optionsType = signal<'C' | 'P'>('C');
  protected readonly optionsExpiration = signal<string>('');
  protected readonly optionsCycle = signal<string | null>(null);
  protected readonly availableCycles = signal<string[]>([]);

  @ViewChild('detailPanelRef') detailPanelRef?: ElementRef<HTMLDivElement>;

  private readonly api = inject(TcgOptionsApi);
  private readonly injector = inject(Injector);

  protected readonly OPTIONS_TABS = OPTIONS_TABS;

  protected readonly isOption = computed(() => tcgIsOptionSelection(this.selected()));
  protected readonly isContinuous = computed(() =>
    tcgIsContinuousSelection(this.selected()),
  );
  protected readonly optionRoot = computed<string | null>(() => {
    const s = this.selected();
    return tcgIsOptionSelection(s) ? s.collection : null;
  });

  private readonly expirationsResource = tcgUseOptionExpirations(
    this.optionRoot,
    this.api,
  );
  protected readonly rootExpirations = this.expirationsResource.expirations;
  protected readonly rootExpirationsLoading = this.expirationsResource.loading;
  protected readonly rootExpirationOptions = tcgReversedExpirations(
    this.rootExpirations,
  );

  // For scroll-into-view: track the id key so re-clicks of the same
  // contract don't re-trigger the scroll.
  private lastContractKey: string | null = null;

  constructor() {
    // Default smile expiration to the LATEST available date once the list
    // loads (or when the user picks a different root). User can change it
    // via the dropdown.
    effect(() => {
      const exps = this.rootExpirations();
      if (exps.length === 0) return;
      const current = this.optionsExpiration();
      if (!current || !exps.includes(current)) {
        this.optionsExpiration.set(exps[exps.length - 1]);
      }
    });

    // Reset selectedContract + view + dates when the user picks a different
    // options root.
    effect(() => {
      const s = this.selected();
      // Re-runs on any selected change; we only care about collection.
      this.selectedContract.set(null);
      this.optionsView.set('chain');
      this.optionsExpiration.set('');
      this.optionsCycle.set(null);
      this.availableCycles.set([]);
      if (tcgIsOptionSelection(s) && s.last_trade_date) {
        this.optionsDate.set(s.last_trade_date);
      }
    });

    // Whenever the smile re-keys, clear cycle state.
    effect(() => {
      this.optionsExpiration();
      this.optionsType();
      this.optionsDate();
      this.optionsCycle.set(null);
      this.availableCycles.set([]);
    });

    // Scroll detail panel into view when selectedContract changes (by id).
    effect(() => {
      const c = this.selectedContract();
      const key = c ? `${c.collection}|${c.instrument_id}` : null;
      if (!key || key === this.lastContractKey) {
        if (!key) this.lastContractKey = null;
        return;
      }
      this.lastContractKey = key;
      // Defer to next render so the panel element is mounted.
      afterNextRender(
        () => {
          const node = this.detailPanelRef?.nativeElement;
          if (node && typeof node.scrollIntoView === 'function') {
            const mql = window.matchMedia?.('(prefers-reduced-motion: reduce)');
            const behavior = mql?.matches ? 'auto' : 'smooth';
            node.scrollIntoView({ behavior, block: 'start' });
          }
        },
        { injector: this.injector },
      );
    });
  }

  ngAfterViewInit(): void {
    /* @ViewChild bound; scroll-into-view handled by effect + afterNextRender. */
  }

  protected onSelect(sel: TcgDataSelection | null): void {
    this.selected.set(sel);
  }

  protected setOptionsView(tab: TcgOptionsViewTab): void {
    this.optionsView.set(tab);
  }

  protected setOptionsDate(v: string): void {
    this.optionsDate.set(v);
  }
  protected setOptionsType(v: string): void {
    if (v === 'C' || v === 'P') this.optionsType.set(v);
  }
  protected setOptionsCycle(v: string): void {
    this.optionsCycle.set(v === '' ? null : v);
  }
  protected setOptionsExpiration(v: string): void {
    this.optionsExpiration.set(v);
  }

  protected onRowClick(contract: TcgContractRef): void {
    this.selectedContract.set(contract);
  }

  protected closeDetail(): void {
    this.selectedContract.set(null);
  }

  protected onSnapshotData(response: TcgChainSnapshotResponse): void {
    if (!response || !Array.isArray(response.series)) return;
    const counts = new Map<string, number>();
    for (const s of response.series) {
      if (!s || !Array.isArray(s.points)) continue;
      for (const p of s.points) {
        const c = p && typeof p.expiration_cycle === 'string' ? p.expiration_cycle : '';
        counts.set(c, (counts.get(c) ?? 0) + 1);
      }
    }
    const cycles = [...counts.keys()].filter((c) => c !== '').sort();
    this.availableCycles.update((prev) => {
      if (prev.length === cycles.length && prev.every((c, i) => c === cycles[i])) {
        return prev;
      }
      return cycles;
    });
    // Auto-select most-populated cycle on first non-null response.
    this.optionsCycle.update((current) => {
      if (current !== null) return current;
      if (cycles.length === 0) return null;
      let best = cycles[0];
      let bestCount = counts.get(best) ?? 0;
      for (const c of cycles) {
        const n = counts.get(c) ?? 0;
        if (n > bestCount) {
          best = c;
          bestCount = n;
        }
      }
      return best;
    });
  }

  // Type guards for template
  protected asOption(sel: TcgDataSelection | null) {
    return tcgIsOptionSelection(sel) ? sel : null;
  }
  protected asContinuous(sel: TcgDataSelection | null) {
    return tcgIsContinuousSelection(sel) ? sel : null;
  }
  protected asInstrument(sel: TcgDataSelection | null) {
    if (!sel) return null;
    if (sel.type === undefined || sel.type === 'instrument') return sel;
    return null;
  }
}
