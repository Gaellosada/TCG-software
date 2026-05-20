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
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { TcgBasketLegRowComponent } from './tcg-basket-leg-row.component';
import { TcgSpotCandidate } from './tcg-spot-leg-picker.component';
import { TcgPersistenceApi } from '../../api/tcg-persistence-api.service';
import {
  TcgBasketAssetClass,
  TcgInlineBasketRef,
  TcgInstrumentLeg,
  TcgSavedBasketRef,
  tcgCollectionsForAssetClass,
} from './types';

interface Leg {
  __id: string;
  instrument: TcgInstrumentLeg;
  weight: number;
}

const ASSET_CLASSES: Array<{ key: TcgBasketAssetClass; label: string }> = [
  { key: 'future', label: 'Future' },
  { key: 'option', label: 'Option' },
  { key: 'index', label: 'Index' },
  { key: 'equity', label: 'Equity' },
];

let legIdCounter = 0;
function nextLegId(): string {
  legIdCounter += 1;
  return `leg-${legIdCounter}`;
}

function makeEmptyLeg(assetClass: TcgBasketAssetClass): Leg {
  if (assetClass === 'future') {
    return {
      __id: nextLegId(),
      instrument: {
        type: 'continuous',
        collection: '',
        adjustment: 'none',
        cycle: null,
        rollOffset: 0,
        strategy: 'front_month',
      },
      weight: 1,
    };
  }
  if (assetClass === 'option') {
    return {
      __id: nextLegId(),
      instrument: {
        type: 'option_stream',
        collection: '',
        option_type: 'C',
        cycle: null,
        maturity: { kind: 'next_third_friday', offset_months: 0 },
        selection: { kind: 'by_moneyness', moneyness: 1.0 },
        stream: 'mid',
      },
      weight: 1,
    };
  }
  return {
    __id: nextLegId(),
    instrument: { type: 'spot', collection: '', instrument_id: '' },
    weight: 1,
  };
}

function isInstrumentRefConfigured(inst: TcgInstrumentLeg): boolean {
  if (!inst || typeof inst !== 'object') return false;
  if (inst.type === 'spot') {
    return !!(inst.collection && inst.instrument_id);
  }
  if (inst.type === 'continuous') {
    return !!inst.collection;
  }
  if (inst.type === 'option_stream') {
    return !!(
      inst.collection &&
      (inst.option_type === 'C' || inst.option_type === 'P') &&
      inst.maturity &&
      inst.selection &&
      inst.stream
    );
  }
  return false;
}

/**
 * Inline basket composer. Mirrors React's `BasketComposer` state machine
 * (`pristine | saved-clean | saved-dirty`) at a Phase A fidelity:
 *   - asset-class switch clears legs (with confirm when any leg is
 *     non-empty);
 *   - per-leg add / remove / weight / instrument-change;
 *   - emits `{type:'basket', kind:'inline', asset_class, legs}` by default;
 *   - emits `{type:'basket', kind:'saved', basket_id}` when a saved basket
 *     was loaded AND not modified since.
 *
 * The "Save as basket…" branch posts to `TcgPersistenceApi.createBasket`
 * and transitions to `saved-clean`.
 */
@Component({
  selector: 'tcg-basket-composer',
  standalone: true,
  imports: [CommonModule, FormsModule, TcgBasketLegRowComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [TcgPersistenceApi],
  template: `
    <div class="tcg-bc" data-testid="basket-composer">
      <div class="tcg-bc__row">
        <label class="tcg-bc__field">
          <span>Saved</span>
          <select
            [value]="selectedSavedId()"
            (change)="onSelectSaved($event)"
            [disabled]="basketsLoading"
            data-testid="basket-saved-select"
          >
            <option value="">— select —</option>
            @for (b of basketList; track b.id) {
              <option [value]="b.id">{{ b.name || b.id }}</option>
            }
          </select>
        </label>
        <label class="tcg-bc__field">
          <span>Asset</span>
          <select
            [value]="assetClass()"
            (change)="requestAssetClassChange($event)"
            data-testid="basket-asset-class-select"
          >
            @for (ac of assetClasses; track ac.key) {
              <option [value]="ac.key">{{ ac.label }}</option>
            }
          </select>
        </label>
      </div>

      @if (basketsError) {
        <div class="tcg-bc__error" data-testid="basket-list-error">{{ basketsError }}</div>
      }

      @if (pendingAssetClass()) {
        <div class="tcg-bc__banner" data-testid="basket-asset-class-confirm">
          <span>Switching asset class will clear all legs. Continue?</span>
          <span class="tcg-bc__banner-actions">
            <button
              type="button"
              class="tcg-bc__cta"
              (click)="confirmAssetClassChange()"
              data-testid="basket-asset-class-confirm-yes"
            >
              Confirm
            </button>
            <button
              type="button"
              class="tcg-bc__cta tcg-bc__cta--secondary"
              (click)="cancelAssetClassChange()"
              data-testid="basket-asset-class-confirm-cancel"
            >
              Cancel
            </button>
          </span>
        </div>
      }

      @if (savedBasket(); as sb) {
        <div class="tcg-bc__banner" data-testid="basket-saved-banner">
          <span>
            @if (dirtySinceSave()) {
              Modified — re-save to keep changes (current selection emits inline).
            } @else {
              ✓ Saved as "{{ sb.name }}"
            }
          </span>
          <button
            type="button"
            class="tcg-bc__cta tcg-bc__cta--secondary"
            (click)="handleUnsave()"
            data-testid="basket-unsave-btn"
          >
            Unsave
          </button>
        </div>
      }

      <div class="tcg-bc__legs" data-testid="basket-legs">
        @for (leg of legs(); track leg.__id; let i = $index) {
          <tcg-basket-leg-row
            [instrument]="leg.instrument"
            [weight]="leg.weight"
            [assetClass]="assetClass()"
            [candidateInstruments]="candidateInstruments()"
            [candidateCollections]="candidateCollections()"
            [optionRoots]="optionRoots"
            [testId]="'basket-leg-' + i"
            (instrumentChange)="onLegInstrument(i, $event)"
            (weightChange)="onLegWeight(i, $event)"
            (remove)="onLegRemove(i)"
          ></tcg-basket-leg-row>
        }
        <button
          type="button"
          class="tcg-bc__add-leg"
          (click)="addLeg()"
          data-testid="basket-add-leg"
        >
          + Add leg
        </button>
      </div>

      <div class="tcg-bc__ctas">
        <button
          type="button"
          class="tcg-bc__cta tcg-bc__cta--secondary"
          (click)="openSaveInput()"
          [disabled]="!hasConfiguredLeg() || saveInputOpen() || (usingSavedRef() && !dirtySinceSave())"
          data-testid="basket-save-btn"
        >
          {{ saveButtonLabel() }}
        </button>
        <button
          type="button"
          class="tcg-bc__cta"
          (click)="handleUseComposition()"
          [disabled]="!hasConfiguredLeg()"
          data-testid="basket-use-btn"
        >
          {{ usingSavedRef() ? 'Use saved basket' : 'Use without saving' }}
        </button>
      </div>
    </div>
  `,
  styles: [
    `
      .tcg-bc {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .tcg-bc__row {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }
      .tcg-bc__field {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 0.8125rem;
        color: var(--text-secondary, #6b7280);
      }
      select {
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
      }
      .tcg-bc__banner {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 8px 12px;
        background: var(--bg-hover, #f3f4f6);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
        font-size: 0.85rem;
      }
      .tcg-bc__banner-actions {
        display: flex;
        gap: 8px;
      }
      .tcg-bc__legs {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .tcg-bc__add-leg {
        background: transparent;
        border: 1px dashed var(--border-primary, #d1d5db);
        color: var(--text-secondary, #6b7280);
        padding: 6px 12px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.85rem;
        align-self: flex-start;
      }
      .tcg-bc__ctas {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
        border-top: 1px solid var(--border-primary, #d1d5db);
        padding-top: 12px;
      }
      .tcg-bc__cta {
        padding: 4px 12px;
        font-size: 0.8125rem;
        border-radius: 4px;
        border: none;
        background: var(--accent, #2563eb);
        color: #fff;
        cursor: pointer;
      }
      .tcg-bc__cta--secondary {
        background: var(--bg-primary, #f9fafb);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
      }
      .tcg-bc__cta:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .tcg-bc__error {
        color: #b91c1c;
        font-size: 0.8125rem;
      }
    `,
  ],
})
export class TcgBasketComposerComponent {
  @Input() allCollections: ReadonlyArray<string> = [];
  @Input() instrumentsByCollection: Record<string, ReadonlyArray<TcgSpotCandidate>> = {};
  @Input() basketList: ReadonlyArray<{ id: string; name?: string; asset_class?: TcgBasketAssetClass; legs?: Array<{ instrument: TcgInstrumentLeg; weight: number }> }> = [];
  @Input() basketsLoading: boolean = false;
  @Input() basketsError: string | null = null;
  @Input() optionRoots: ReadonlyArray<string> = [];

  @Output() emit = new EventEmitter<TcgSavedBasketRef | TcgInlineBasketRef>();

  readonly assetClasses = ASSET_CLASSES;

  readonly assetClass = signal<TcgBasketAssetClass>('future');
  readonly legs = signal<Leg[]>([makeEmptyLeg('future')]);
  readonly selectedSavedId = signal<string>('');
  readonly savedBasket = signal<{ id: string; name: string } | null>(null);
  readonly dirtySinceSave = signal<boolean>(false);
  readonly pendingAssetClass = signal<TcgBasketAssetClass | null>(null);
  readonly saveInputOpen = signal<boolean>(false);

  private readonly persistence = inject(TcgPersistenceApi);

  readonly candidateCollections = computed(() =>
    tcgCollectionsForAssetClass(this.assetClass(), this.allCollections),
  );
  readonly candidateInstruments = computed(() => {
    const out: TcgSpotCandidate[] = [];
    for (const coll of this.candidateCollections()) {
      const items = this.instrumentsByCollection[coll] ?? [];
      for (const inst of items) {
        out.push({ symbol: inst.symbol, collection: coll });
      }
    }
    return out;
  });
  readonly hasConfiguredLeg = computed(() =>
    this.legs().some(
      (l) => isInstrumentRefConfigured(l.instrument) && Number.isFinite(l.weight) && l.weight !== 0,
    ),
  );
  readonly usingSavedRef = computed(() => !!this.savedBasket() && !this.dirtySinceSave());

  saveButtonLabel(): string {
    if (this.usingSavedRef()) return 'Saved ✓';
    if (this.savedBasket() && this.dirtySinceSave()) return 'Re-save…';
    return 'Save as basket…';
  }

  private markDirtyIfSaved(): void {
    if (this.savedBasket()) this.dirtySinceSave.set(true);
  }

  onLegInstrument(idx: number, instrument: TcgInstrumentLeg): void {
    const next = this.legs().slice();
    next[idx] = { ...next[idx], instrument };
    this.legs.set(next);
    this.markDirtyIfSaved();
  }

  onLegWeight(idx: number, weight: number): void {
    const next = this.legs().slice();
    next[idx] = { ...next[idx], weight };
    this.legs.set(next);
    this.markDirtyIfSaved();
  }

  onLegRemove(idx: number): void {
    const next = this.legs().slice();
    next.splice(idx, 1);
    this.legs.set(next.length === 0 ? [makeEmptyLeg(this.assetClass())] : next);
    this.markDirtyIfSaved();
  }

  addLeg(): void {
    this.legs.set([...this.legs(), makeEmptyLeg(this.assetClass())]);
    this.markDirtyIfSaved();
  }

  requestAssetClassChange(event: Event): void {
    const next = (event.target as HTMLSelectElement).value as TcgBasketAssetClass;
    if (next === this.assetClass()) return;
    const hasNonEmpty = this.legs().some((l) => {
      const inst = l.instrument;
      if (inst.type === 'spot') return !!(inst.collection || inst.instrument_id);
      if (inst.type === 'continuous') return !!inst.collection;
      if (inst.type === 'option_stream') return !!inst.collection;
      return false;
    });
    if (hasNonEmpty) {
      this.pendingAssetClass.set(next);
    } else {
      this.assetClass.set(next);
      this.legs.set([makeEmptyLeg(next)]);
      this.markDirtyIfSaved();
    }
  }

  confirmAssetClassChange(): void {
    const next = this.pendingAssetClass();
    if (!next) return;
    this.assetClass.set(next);
    this.legs.set([makeEmptyLeg(next)]);
    this.pendingAssetClass.set(null);
    this.markDirtyIfSaved();
  }
  cancelAssetClassChange(): void {
    this.pendingAssetClass.set(null);
  }

  onSelectSaved(event: Event): void {
    const id = (event.target as HTMLSelectElement).value;
    this.selectedSavedId.set(id);
    if (!id) {
      this.savedBasket.set(null);
      this.dirtySinceSave.set(false);
      return;
    }
    const found = this.basketList.find((b) => b.id === id);
    if (!found) return;
    const ac = (
      found.asset_class === 'future' ||
      found.asset_class === 'option' ||
      found.asset_class === 'index' ||
      found.asset_class === 'equity'
    )
      ? found.asset_class
      : this.assetClass();
    this.assetClass.set(ac);
    this.legs.set(
      (found.legs ?? []).map((l) => ({
        __id: nextLegId(),
        instrument: l.instrument,
        weight: typeof l.weight === 'number' ? l.weight : 1,
      })),
    );
    this.savedBasket.set({ id: found.id, name: found.name || found.id });
    this.dirtySinceSave.set(false);
  }

  handleUnsave(): void {
    this.savedBasket.set(null);
    this.dirtySinceSave.set(false);
    this.selectedSavedId.set('');
  }

  openSaveInput(): void {
    this.saveInputOpen.set(true);
  }

  handleUseComposition(): void {
    if (!this.hasConfiguredLeg()) return;
    const sb = this.savedBasket();
    if (sb && !this.dirtySinceSave()) {
      this.emit.emit({ type: 'basket', kind: 'saved', basket_id: sb.id });
      return;
    }
    const emittableLegs = this.legs()
      .filter(
        (l) =>
          isInstrumentRefConfigured(l.instrument) && Number.isFinite(l.weight) && l.weight !== 0,
      )
      .map((l) => ({ instrument: l.instrument, weight: l.weight }));
    this.emit.emit({
      type: 'basket',
      kind: 'inline',
      asset_class: this.assetClass(),
      legs: emittableLegs,
    });
  }
}
