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
import { FormsModule } from '@angular/forms';
import { TcgDataApi } from '../../api/tcg-data-api.service';
import { TcgContinuousInstrumentRef } from './types';

/**
 * Continuous-series spec picker — adjustment / cycle / rollOffset.
 * Mirrors React's in-file `ContinuousSpecPicker`. Single source of truth
 * for the three continuous controls — reused by the futures drill-down
 * AND per-leg by the basket composer for `future` asset_class.
 *
 * When `availableCycles` is supplied, the parent owns cycle loading.
 * When undefined, this component loads cycles via `TcgDataApi` keyed off
 * `value.collection` (basket-leg case).
 */
@Component({
  selector: 'tcg-continuous-spec-picker',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [TcgDataApi],
  template: `
    <div class="tcg-cs-picker" data-testid="continuous-spec-picker">
      <label class="tcg-cs-picker__field">
        <span>Adjustment</span>
        <select
          [value]="value.adjustment || 'none'"
          (change)="onAdjustment($event)"
          data-testid="continuous-spec-picker-adjustment"
        >
          <option value="none">None</option>
          <option value="ratio">Ratio</option>
          <option value="difference">Difference</option>
        </select>
      </label>
      <label class="tcg-cs-picker__field">
        <span>Cycle</span>
        <select
          [value]="value.cycle == null ? '' : value.cycle"
          (change)="onCycle($event)"
          data-testid="continuous-spec-picker-cycle"
        >
          <option value="">All</option>
          @for (c of cycles(); track c) {
            <option [value]="c">{{ c }}</option>
          }
        </select>
      </label>
      <label class="tcg-cs-picker__field">
        <span>Roll Offset (days)</span>
        <input
          type="number"
          [value]="value.rollOffset || 0"
          [min]="0"
          [max]="30"
          (input)="onRollOffset($event)"
          style="width:56px"
          data-testid="continuous-spec-picker-roll-offset"
        />
      </label>
    </div>
  `,
  styles: [
    `
      .tcg-cs-picker {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }
      .tcg-cs-picker__field {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 0.8125rem;
        color: var(--text-secondary, #6b7280);
      }
      select,
      input {
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
      }
    `,
  ],
})
export class TcgContinuousSpecPickerComponent implements OnChanges {
  @Input({ required: true }) value!: TcgContinuousInstrumentRef;
  @Input() availableCycles?: ReadonlyArray<string>;
  /** Reserved for future per-class dispatch. */
  @Input() assetClass: 'future' | 'option' = 'future';

  @Output() valueChange = new EventEmitter<TcgContinuousInstrumentRef>();

  private readonly dataApi = inject(TcgDataApi);
  private readonly internalCycles = signal<ReadonlyArray<string>>([]);

  cycles(): ReadonlyArray<string> {
    return this.availableCycles ?? this.internalCycles();
  }

  ngOnChanges(changes: SimpleChanges): void {
    // Self-load cycles only when the parent does not own the list.
    if (this.availableCycles !== undefined) {
      return;
    }
    if (changes['value']) {
      const coll = this.value?.collection ?? '';
      if (!coll) {
        this.internalCycles.set([]);
        return;
      }
      this.dataApi.getAvailableCycles(coll).subscribe({
        next: (cycles) => this.internalCycles.set(cycles ?? []),
        error: () => this.internalCycles.set([]),
      });
    }
  }

  private emit(patch: Partial<TcgContinuousInstrumentRef>): void {
    this.valueChange.emit({
      type: 'continuous',
      collection: this.value.collection,
      adjustment: this.value.adjustment || 'none',
      cycle: this.value.cycle ?? null,
      rollOffset: Number.isFinite(this.value.rollOffset) ? this.value.rollOffset : 0,
      strategy: 'front_month',
      ...patch,
    });
  }

  onAdjustment(event: Event): void {
    this.emit({ adjustment: (event.target as HTMLSelectElement).value });
  }

  onCycle(event: Event): void {
    const v = (event.target as HTMLSelectElement).value;
    this.emit({ cycle: v === '' ? null : v });
  }

  onRollOffset(event: Event): void {
    const raw = parseInt((event.target as HTMLInputElement).value, 10) || 0;
    this.emit({ rollOffset: Math.max(0, Math.min(30, raw)) });
  }
}
