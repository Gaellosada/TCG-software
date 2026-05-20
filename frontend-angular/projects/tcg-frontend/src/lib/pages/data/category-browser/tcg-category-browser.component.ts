import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnInit,
  Output,
  effect,
  inject,
  signal,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { TcgDataApi, TcgInstrumentItem } from '../../../api/tcg-data-api.service';
import {
  TcgOptionRootInfo,
  TcgOptionsApi,
} from '../../../api/tcg-options-api.service';
import { TcgDataSelection } from '../data-types';

interface CategoryStatic {
  key: 'indexes' | 'assets' | 'futures' | 'options';
  label: string;
  color: string;
  collections?: string[];
  dynamicFutures?: boolean;
  dynamicOptions?: boolean;
}

const CATEGORY_CONFIG: CategoryStatic[] = [
  { key: 'indexes', label: 'Indexes', color: 'var(--cat-indexes)', collections: ['INDEX'] },
  {
    key: 'assets',
    label: 'Assets',
    color: 'var(--cat-assets)',
    collections: ['ETF', 'FOREX', 'FUND'],
  },
  { key: 'futures', label: 'Futures', color: 'var(--cat-futures)', dynamicFutures: true },
  { key: 'options', label: 'Options', color: 'var(--cat-options)', dynamicOptions: true },
];

interface ResolvedCategory extends CategoryStatic {
  isFutures?: boolean;
  isOptions?: boolean;
  futCollections?: string[];
  optionRoots?: TcgOptionRootInfo[];
  groups?: Array<{
    collection: string;
    instruments: Array<{ symbol: string; collection: string }>;
  }>;
}

/**
 * Category browser sidebar — left rail of the Data page. Mirrors React's
 * `CategoryBrowser.jsx`. Lazy-loads instrument lists for futures contract
 * details on first expand.
 *
 * G3/G4/G8: standalone, `tcg-` selector + `Tcg*` class.
 */
@Component({
  selector: 'tcg-category-browser',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './tcg-category-browser.component.html',
  styleUrls: ['./tcg-category-browser.component.css'],
})
export class TcgCategoryBrowserComponent implements OnInit {
  @Input() selected: TcgDataSelection | null = null;
  @Output() readonly select = new EventEmitter<TcgDataSelection | null>();

  protected readonly categories = signal<ResolvedCategory[]>([]);
  protected readonly expanded = signal<Record<string, boolean>>({
    indexes: false,
    assets: false,
    futures: false,
    options: false,
  });
  protected readonly expandedFutGroups = signal<Record<string, boolean>>({});
  protected readonly contractsExpanded = signal<Record<string, boolean>>({});
  protected readonly contractsData = signal<Record<string, TcgInstrumentItem[]>>({});
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  private readonly dataApi = inject(TcgDataApi);
  private readonly optionsApi = inject(TcgOptionsApi);

  // Track input changes — Angular doesn't re-run an effect for plain @Input
  // setters, so we mirror into a signal via ngOnChanges-equivalent.
  private readonly selectedSig = signal<TcgDataSelection | null>(null);

  constructor() {
    // Auto-collapse futures groups whose collection doesn't own the
    // current selection (mirrors React useEffect on `selected`).
    effect(() => {
      const sel = this.selectedSig();
      if (!sel) {
        this.expandedFutGroups.set({});
        this.contractsExpanded.set({});
        return;
      }
      const selCollection = sel.collection;
      this.expandedFutGroups.update((prev) => {
        const next: Record<string, boolean> = {};
        for (const key of Object.keys(prev)) {
          next[key] = key === selCollection;
        }
        if (selCollection) next[selCollection] = true;
        return next;
      });
      this.contractsExpanded.update((prev) => {
        const next: Record<string, boolean> = {};
        const isInstrument = sel.type === undefined || sel.type === 'instrument';
        for (const key of Object.keys(prev)) {
          next[key] = isInstrument && key === selCollection;
        }
        return next;
      });
    });
  }

  ngOnChanges(): void {
    this.selectedSig.set(this.selected);
  }

  async ngOnInit(): Promise<void> {
    this.selectedSig.set(this.selected);
    try {
      this.loading.set(true);
      this.error.set(null);
      const rawCollections = await firstValueFrom(this.dataApi.listCollections());
      // The backend ships either string[] or { name, display_name }[]; normalise
      // to a name-list for the filter checks below.
      const collections: string[] = rawCollections.map((c) => {
        if (typeof c === 'string') return c;
        const rec = c as { name?: string; collection?: string };
        return rec.name ?? rec.collection ?? '';
      });

      const result: ResolvedCategory[] = await Promise.all(
        CATEGORY_CONFIG.map(async (cat) => {
          if (cat.dynamicFutures) {
            const futCollections = collections.filter((c) => c.startsWith('FUT_'));
            return { ...cat, futCollections, isFutures: true };
          }
          if (cat.dynamicOptions) {
            const resp = await firstValueFrom(this.optionsApi.getOptionRoots());
            return { ...cat, optionRoots: resp.roots ?? [], isOptions: true };
          }
          const available = (cat.collections ?? []).filter((c) => collections.includes(c));
          const groups = await Promise.all(
            available.map(async (collName) => {
              const res = await firstValueFrom(this.dataApi.listInstruments(collName));
              return {
                collection: collName,
                instruments: (res.items ?? []).map((item) => ({
                  symbol: String(item.symbol ?? item.instrument_id ?? ''),
                  collection: String((item as Record<string, unknown>)['collection'] ?? collName),
                })),
              };
            }),
          );
          return { ...cat, groups, isFutures: false };
        }),
      );
      this.categories.set(result);
      this.loading.set(false);
    } catch (err: unknown) {
      this.error.set(err instanceof Error ? err.message : String(err));
      this.loading.set(false);
    }
  }

  toggleCategory(key: string): void {
    this.expanded.update((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  handleFutGroupClick(collName: string): void {
    const wasExpanded = this.expandedFutGroups()[collName];
    if (wasExpanded) {
      this.expandedFutGroups.update((prev) => ({ ...prev, [collName]: false }));
      this.contractsExpanded.update((prev) => ({ ...prev, [collName]: false }));
      if (this.selected?.collection === collName) {
        this.select.emit(null);
      }
    } else {
      this.select.emit({ type: 'continuous', collection: collName });
    }
  }

  async toggleContracts(collName: string): Promise<void> {
    const wasExpanded = this.contractsExpanded()[collName];
    this.contractsExpanded.update((prev) => ({ ...prev, [collName]: !wasExpanded }));
    if (!wasExpanded && !this.contractsData()[collName]) {
      try {
        const res = await firstValueFrom(
          this.dataApi.listInstruments(collName, { skip: 0, limit: 500 }),
        );
        this.contractsData.update((prev) => ({ ...prev, [collName]: res.items ?? [] }));
      } catch {
        this.contractsData.update((prev) => ({ ...prev, [collName]: [] }));
      }
    }
  }

  isSelected(
    sel: TcgDataSelection | null,
    type: 'continuous' | 'instrument',
    symbol: string | null,
    collection: string,
  ): boolean {
    if (!sel) return false;
    if (type === 'continuous') {
      return sel.type === 'continuous' && sel.collection === collection;
    }
    if (sel.type === undefined || sel.type === 'instrument') {
      return sel.symbol === symbol && sel.collection === collection;
    }
    return false;
  }

  selectInstrument(symbol: string, collection: string): void {
    this.select.emit({ symbol, collection });
  }

  selectContinuous(collection: string): void {
    this.select.emit({ type: 'continuous', collection });
  }

  selectOptionRoot(root: TcgOptionRootInfo): void {
    const collection = String(root['collection'] ?? root.name);
    this.select.emit({
      type: 'option',
      collection,
      instrument_id: null,
      expiry: null,
      strike: null,
      optionType: null,
      last_trade_date: root.last_trade_date ?? null,
      expiration_last: root.expiration_last ?? null,
    });
  }

  /** Greek-badge classification. Mirrors React's `renderGreeksBadge`. */
  greekBadgeKind(root: TcgOptionRootInfo): 'full' | 'partial' | 'computed' | null {
    const ratio =
      typeof root.stored_greeks_ratio === 'number'
        ? root.stored_greeks_ratio
        : root.has_greeks
          ? 1
          : 0;
    const canCompute = root.has_computed_greeks ?? false;
    if (ratio >= 0.9) return 'full';
    if (ratio >= 0.1) return 'partial';
    if (canCompute) return 'computed';
    return null;
  }

  trackCategory(_idx: number, cat: ResolvedCategory): string {
    return cat.key;
  }
  trackByCollection(_idx: number, c: string): string {
    return c;
  }
  trackByGroup(_idx: number, g: { collection: string }): string {
    return g.collection;
  }
  trackByInstrument(_idx: number, inst: { symbol: string }): string {
    return inst.symbol;
  }
  trackByRoot(_idx: number, root: TcgOptionRootInfo): string {
    return String(root['collection'] ?? root.name);
  }
  trackItem(_idx: number, item: TcgInstrumentItem): string {
    return String(item.symbol ?? item.instrument_id ?? '');
  }
}
