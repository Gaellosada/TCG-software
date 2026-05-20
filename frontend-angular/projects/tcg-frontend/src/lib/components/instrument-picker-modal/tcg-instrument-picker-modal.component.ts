import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { A11yModule } from '@angular/cdk/a11y';
import { TcgDataApi, TcgInstrumentItem } from '../../api/tcg-data-api.service';
import { TcgBasketComposerComponent } from './tcg-basket-composer.component';
import { TcgContinuousSpecPickerComponent } from './tcg-continuous-spec-picker.component';
import { TcgOptionStreamPickerComponent } from './tcg-option-stream-picker.component';
import { TcgSpotCandidate } from './tcg-spot-leg-picker.component';
import { TcgContinuousInstrumentRef, TcgInstrumentDescriptor, TcgOptionStreamRef } from './types';

interface CategoryConfig {
  key: string;
  label: string;
  collections?: ReadonlyArray<string>;
  dynamicFutures?: boolean;
  dynamicOptions?: boolean;
  dynamicBaskets?: boolean;
}

const CATEGORY_CONFIG: ReadonlyArray<CategoryConfig> = [
  { key: 'indexes', label: 'Indexes', collections: ['INDEX'] },
  { key: 'assets', label: 'Assets', collections: ['ETF', 'FOREX', 'FUND'] },
  { key: 'futures', label: 'Futures', dynamicFutures: true },
  { key: 'options', label: 'Options', dynamicOptions: true },
  { key: 'baskets', label: 'Baskets', dynamicBaskets: true },
];

/**
 * Top-level instrument picker modal. Mirrors React's
 * `InstrumentPickerModal.jsx`:
 *   - categorised view (Indexes / Assets / Futures / Options / Baskets);
 *   - drill-downs for Futures (continuous spec), Options (stream form),
 *     Baskets (inline composer);
 *   - ESC closes; backdrop click closes;
 *   - emits a discriminated-union descriptor via `(selected)` and
 *     calls `(close)` after each emit.
 *
 * Phase A scaffold: the structural shape matches React's modal and the
 * descriptor emit surface is locked. Sub-pickers (continuous, option
 * stream, basket composer) are themselves Phase A scaffolds for the
 * deeper state machines; consumers in Wave I get the wiring right today
 * and the full UX fills in as later waves port Signals/Portfolio/etc.
 *
 * REVIEW: Phase A scaffold — sub-flows track the React state machine at
 * the level of detail Workers B + C need (Data + Settings DON'T use this
 * modal). Full fidelity ships when Workers porting Portfolio / Indicators
 * / Signals consume it.
 */
@Component({
  selector: 'tcg-instrument-picker-modal',
  standalone: true,
  imports: [
    CommonModule,
    A11yModule,
    TcgContinuousSpecPickerComponent,
    TcgOptionStreamPickerComponent,
    TcgBasketComposerComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [TcgDataApi],
  template: `
    @if (isOpen) {
      <div
        class="tcg-ipm__overlay"
        (mousedown)="onOverlayMouseDown($event)"
        role="dialog"
        aria-modal="true"
        [attr.aria-label]="title || 'Select Instrument'"
      >
        <div
          class="tcg-ipm__modal"
          cdkTrapFocus
          [cdkTrapFocusAutoCapture]="true"
          (keydown.escape)="close.emit()"
        >
          <div class="tcg-ipm__header">
            <div class="tcg-ipm__header-left">
              @if (inDrillDown()) {
                <button type="button" class="tcg-ipm__back-btn" (click)="onBack()">←</button>
              }
              <h3 class="tcg-ipm__title">{{ headerTitle() }}</h3>
            </div>
            <button
              type="button"
              class="tcg-ipm__close-btn"
              (click)="close.emit()"
              aria-label="Close"
            >
              ×
            </button>
          </div>
          <div class="tcg-ipm__body">
            @if (collectionsLoading()) {
              <div class="tcg-ipm__state">Loading...</div>
            }
            @if (collectionsError(); as err) {
              <div class="tcg-ipm__error">{{ err }}</div>
            }

            @if (inOptionsDrillDown()) {
              <div class="tcg-ipm__section">
                <tcg-option-stream-picker
                  [value]="optionStreamValue()"
                  [availableRoots]="optionRoots()"
                  (valueChange)="optionStreamValue.set($event)"
                ></tcg-option-stream-picker>
                <button
                  type="button"
                  class="tcg-ipm__cta"
                  (click)="confirmOptionStream()"
                  [disabled]="!optionStreamValue()"
                  data-testid="option-stream-confirm"
                >
                  Confirm
                </button>
              </div>
            } @else if (inFutDrillDown()) {
              <div class="tcg-ipm__section">
                <p>
                  <strong>{{ selectedFutCollection() }}</strong> will be added as a continuous
                  rolled series (front month).
                </p>
                <tcg-continuous-spec-picker
                  [value]="futSpec()"
                  (valueChange)="futSpec.set($event)"
                  assetClass="future"
                ></tcg-continuous-spec-picker>
                <button type="button" class="tcg-ipm__cta" (click)="confirmContinuous()">
                  Select Continuous Series
                </button>
              </div>
            } @else if (inBasketComposer()) {
              <tcg-basket-composer
                [allCollections]="allCollections()"
                [instrumentsByCollection]="instrumentsByCollection()"
                [basketList]="basketList()"
                [optionRoots]="optionRoots()"
                (emit)="onBasketEmit($event)"
              ></tcg-basket-composer>
            } @else {
              @for (cat of visibleStaticCategories(); track cat.key) {
                <div class="tcg-ipm__group">
                  <button
                    type="button"
                    class="tcg-ipm__group-toggle"
                    (click)="toggleCategory(cat.key)"
                  >
                    <span class="tcg-ipm__group-label">{{ cat.label }}</span>
                    <span class="tcg-ipm__chevron">{{
                      expanded()[cat.key] ? '▾' : '▸'
                    }}</span>
                  </button>
                  @if (expanded()[cat.key]) {
                    <ul class="tcg-ipm__instrument-list">
                      @for (inst of instrumentsForCategory(cat); track inst.collection + '|' + inst.symbol) {
                        <li
                          class="tcg-ipm__instrument-item"
                          role="button"
                          tabindex="0"
                          (click)="emitSpot(inst.symbol, inst.collection)"
                          (keydown.enter)="emitSpot(inst.symbol, inst.collection)"
                        >
                          {{ inst.symbol }}
                        </li>
                      }
                    </ul>
                  }
                </div>
              }
              @if (futuresVisible()) {
                <div class="tcg-ipm__group">
                  <button
                    type="button"
                    class="tcg-ipm__group-toggle"
                    (click)="toggleCategory('futures')"
                  >
                    <span class="tcg-ipm__group-label">Futures</span>
                    <span class="tcg-ipm__chevron">{{ expanded()['futures'] ? '▾' : '▸' }}</span>
                  </button>
                  @if (expanded()['futures']) {
                    <ul class="tcg-ipm__instrument-list">
                      @for (coll of futCollections(); track coll) {
                        <li
                          class="tcg-ipm__instrument-item"
                          role="button"
                          tabindex="0"
                          (click)="selectedFutCollection.set(coll)"
                          (keydown.enter)="selectedFutCollection.set(coll)"
                        >
                          {{ coll }} ›
                        </li>
                      }
                    </ul>
                  }
                </div>
              }
              @if (optionsVisible()) {
                <div class="tcg-ipm__group">
                  <button
                    type="button"
                    class="tcg-ipm__group-toggle"
                    data-testid="picker-options-toggle"
                    (click)="enterOptionsDrillDown()"
                  >
                    <span class="tcg-ipm__group-label">Options ›</span>
                  </button>
                </div>
              }
              @if (basketsVisible()) {
                <div class="tcg-ipm__group">
                  <button
                    type="button"
                    class="tcg-ipm__group-toggle"
                    data-testid="picker-baskets-toggle"
                    (click)="enterBasketComposer()"
                  >
                    <span class="tcg-ipm__group-label">Baskets ›</span>
                  </button>
                </div>
              }
            }
          </div>
        </div>
      </div>
    }
  `,
  styles: [
    `
      .tcg-ipm__overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
      }
      .tcg-ipm__modal {
        background: var(--bg-surface, #fff);
        border-radius: 8px;
        max-width: 720px;
        width: 90%;
        max-height: 85vh;
        display: flex;
        flex-direction: column;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
      }
      .tcg-ipm__header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border-bottom: 1px solid var(--border-primary, #e5e7eb);
      }
      .tcg-ipm__header-left {
        display: inline-flex;
        align-items: center;
        gap: 8px;
      }
      .tcg-ipm__title {
        margin: 0;
        font-size: 1rem;
      }
      .tcg-ipm__back-btn,
      .tcg-ipm__close-btn {
        background: transparent;
        border: none;
        font-size: 1.2rem;
        cursor: pointer;
        color: var(--text-secondary, #6b7280);
      }
      .tcg-ipm__body {
        padding: 12px 16px;
        overflow-y: auto;
        flex: 1;
      }
      .tcg-ipm__group {
        margin-bottom: 8px;
      }
      .tcg-ipm__group-toggle {
        width: 100%;
        background: transparent;
        border: 1px solid var(--border-primary, #e5e7eb);
        padding: 8px 12px;
        text-align: left;
        cursor: pointer;
        border-radius: 4px;
        display: flex;
        justify-content: space-between;
        font-size: 0.875rem;
      }
      .tcg-ipm__instrument-list {
        list-style: none;
        padding: 0;
        margin: 6px 0 0;
        border: 1px solid var(--border-primary, #e5e7eb);
        border-radius: 4px;
      }
      .tcg-ipm__instrument-item {
        padding: 6px 12px;
        cursor: pointer;
        font-size: 0.875rem;
        border-bottom: 1px solid var(--border-primary, #e5e7eb);
      }
      .tcg-ipm__instrument-item:last-child {
        border-bottom: none;
      }
      .tcg-ipm__instrument-item:hover {
        background: var(--bg-hover, #f3f4f6);
      }
      .tcg-ipm__section {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .tcg-ipm__cta {
        align-self: flex-end;
        padding: 6px 14px;
        background: var(--accent, #2563eb);
        color: #fff;
        border: none;
        border-radius: 4px;
        font-size: 0.8125rem;
        cursor: pointer;
      }
      .tcg-ipm__cta:disabled {
        opacity: 0.5;
      }
      .tcg-ipm__state {
        padding: 12px;
        color: var(--text-secondary, #6b7280);
      }
      .tcg-ipm__error {
        padding: 12px;
        color: #b91c1c;
      }
    `,
  ],
})
export class TcgInstrumentPickerModalComponent implements OnChanges {
  @Input({ required: true }) isOpen!: boolean;
  @Input() title?: string;
  @Input() hiddenCategories: ReadonlyArray<string> = [];
  @Input() allowBaskets: boolean = false;

  @Output() selected = new EventEmitter<TcgInstrumentDescriptor>();
  @Output() close = new EventEmitter<void>();

  private readonly dataApi = inject(TcgDataApi);

  // State signals.
  readonly allCollections = signal<string[]>([]);
  readonly collectionsLoading = signal<boolean>(false);
  readonly collectionsError = signal<string | null>(null);
  readonly instrumentsByCollection = signal<Record<string, TcgSpotCandidate[]>>({});
  readonly expanded = signal<Record<string, boolean>>({});

  readonly selectedFutCollection = signal<string | null>(null);
  readonly futSpec = signal<TcgContinuousInstrumentRef>({
    type: 'continuous',
    collection: '',
    strategy: 'front_month',
    adjustment: 'none',
    cycle: null,
    rollOffset: 2,
  });

  readonly inOptionsDrillDown = signal<boolean>(false);
  readonly optionStreamValue = signal<TcgOptionStreamRef | null>(null);
  readonly optionRoots = signal<string[]>([]);

  readonly inBasketComposer = signal<boolean>(false);
  readonly basketList = signal<
    Array<{
      id: string;
      name?: string;
      asset_class?: 'future' | 'option' | 'index' | 'equity';
      legs?: Array<{ instrument: any; weight: number }>;
    }>
  >([]);

  inFutDrillDown(): boolean {
    return this.selectedFutCollection() !== null;
  }
  inDrillDown(): boolean {
    return this.inFutDrillDown() || this.inOptionsDrillDown() || this.inBasketComposer();
  }

  visibleStaticCategories(): CategoryConfig[] {
    return CATEGORY_CONFIG.filter((c) => {
      if (this.hiddenCategories.includes(c.key)) return false;
      if (c.key === 'baskets' && !this.allowBaskets) return false;
      return !c.dynamicFutures && !c.dynamicOptions && !c.dynamicBaskets;
    });
  }

  futuresVisible(): boolean {
    return !this.hiddenCategories.includes('futures');
  }
  optionsVisible(): boolean {
    return !this.hiddenCategories.includes('options');
  }
  basketsVisible(): boolean {
    return this.allowBaskets && !this.hiddenCategories.includes('baskets');
  }

  futCollections(): string[] {
    return this.allCollections().filter((c) => c.startsWith('FUT_'));
  }

  instrumentsForCategory(cat: CategoryConfig): TcgSpotCandidate[] {
    const map = this.instrumentsByCollection();
    return (cat.collections ?? []).flatMap((coll) => map[coll] ?? []);
  }

  headerTitle(): string {
    if (this.inFutDrillDown()) return this.selectedFutCollection() ?? '';
    if (this.inOptionsDrillDown()) return 'Options';
    if (this.inBasketComposer()) return 'Basket Composer';
    return this.title || 'Select Instrument';
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['isOpen']) {
      if (this.isOpen) {
        this.loadCollections();
      } else {
        // Reset drill-downs on close.
        this.selectedFutCollection.set(null);
        this.inOptionsDrillDown.set(false);
        this.optionStreamValue.set(null);
        this.inBasketComposer.set(false);
        this.expanded.set({});
      }
    }
  }

  private loadCollections(): void {
    this.collectionsLoading.set(true);
    this.collectionsError.set(null);
    this.dataApi.listCollections().subscribe({
      next: (cols) => {
        // The API returns either string[] or {name:string}[] depending on
        // backend version — handle both.
        const names = cols.map((c) =>
          typeof c === 'string' ? c : ((c as { name?: string }).name ?? ''),
        ).filter((s): s is string => !!s);
        this.allCollections.set(names);
        // Eager-load static-category collections.
        const staticColls = CATEGORY_CONFIG.filter(
          (c) => !c.dynamicFutures && !c.dynamicOptions && !c.dynamicBaskets,
        )
          .flatMap((c) => c.collections ?? [])
          .filter((c) => names.includes(c));
        if (staticColls.length === 0) {
          this.collectionsLoading.set(false);
          return;
        }
        // Parallel-fetch instruments for each.
        let remaining = staticColls.length;
        const map: Record<string, TcgSpotCandidate[]> = {};
        for (const coll of staticColls) {
          this.dataApi.listInstruments(coll, { skip: 0, limit: 500 }).subscribe({
            next: (res) => {
              map[coll] = (res.items ?? []).map((i: TcgInstrumentItem) => ({
                symbol: (i.symbol || i.instrument_id || '') as string,
                collection: coll,
              }));
            },
            error: () => {
              map[coll] = [];
            },
            complete: () => {
              remaining -= 1;
              if (remaining === 0) {
                this.instrumentsByCollection.set({ ...map });
                this.collectionsLoading.set(false);
              }
            },
          });
        }
      },
      error: (err: unknown) => {
        this.collectionsError.set(err instanceof Error ? err.message : String(err));
        this.collectionsLoading.set(false);
      },
    });
  }

  toggleCategory(key: string): void {
    const next = { ...this.expanded() };
    next[key] = !next[key];
    this.expanded.set(next);
  }

  emitSpot(symbol: string, collection: string): void {
    this.selected.emit({ type: 'spot', collection, instrument_id: symbol });
    this.close.emit();
  }

  confirmContinuous(): void {
    const coll = this.selectedFutCollection();
    if (!coll) return;
    this.selected.emit({ ...this.futSpec(), collection: coll });
    this.close.emit();
  }

  enterOptionsDrillDown(): void {
    this.inOptionsDrillDown.set(true);
  }
  enterBasketComposer(): void {
    this.inBasketComposer.set(true);
  }

  confirmOptionStream(): void {
    const v = this.optionStreamValue();
    if (!v) return;
    this.selected.emit(v);
    this.close.emit();
  }

  onBasketEmit(descriptor: TcgInstrumentDescriptor): void {
    this.selected.emit(descriptor);
    this.close.emit();
  }

  onBack(): void {
    if (this.inFutDrillDown()) {
      this.selectedFutCollection.set(null);
      return;
    }
    if (this.inOptionsDrillDown()) {
      this.inOptionsDrillDown.set(false);
      this.optionStreamValue.set(null);
      return;
    }
    if (this.inBasketComposer()) {
      this.inBasketComposer.set(false);
    }
  }

  onOverlayMouseDown(event: MouseEvent): void {
    if (event.target === event.currentTarget) this.close.emit();
  }
}
