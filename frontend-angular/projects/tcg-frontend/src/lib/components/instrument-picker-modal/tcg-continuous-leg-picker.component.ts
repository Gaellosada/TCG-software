import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { TcgContinuousSpecPickerComponent } from './tcg-continuous-spec-picker.component';
import { TcgContinuousInstrumentRef } from './types';

/**
 * Continuous (future) leg picker — collection select + spec picker.
 * Mirrors React's `ContinuousLegPicker`. The collection dropdown is
 * scoped to `FUT_*` collections (the parent passes in candidates);
 * `<tcg-continuous-spec-picker>` handles adjustment / cycle / rollOffset
 * and loads its own cycle list when no `availableCycles` is supplied.
 */
@Component({
  selector: 'tcg-continuous-leg-picker',
  standalone: true,
  imports: [CommonModule, FormsModule, TcgContinuousSpecPickerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tcg-clp" [attr.data-testid]="testId">
      <select
        class="tcg-clp__select"
        [value]="instrument.collection || ''"
        (change)="onCollection($event)"
        [attr.data-testid]="testId + '-collection-select'"
      >
        <option value="">— pick a collection —</option>
        @for (c of candidateCollections; track c) {
          <option [value]="c">{{ c }}</option>
        }
      </select>
      <tcg-continuous-spec-picker
        [value]="instrument"
        (valueChange)="onSpecChange($event)"
        assetClass="future"
      ></tcg-continuous-spec-picker>
    </div>
  `,
  styles: [
    `
      .tcg-clp {
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .tcg-clp__select {
        width: 100%;
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
      }
    `,
  ],
})
export class TcgContinuousLegPickerComponent {
  @Input({ required: true }) instrument!: TcgContinuousInstrumentRef;
  @Input({ required: true }) candidateCollections!: ReadonlyArray<string>;
  @Input() testId: string = 'continuous-leg';

  @Output() instrumentChange = new EventEmitter<TcgContinuousInstrumentRef>();

  onCollection(event: Event): void {
    const collection = (event.target as HTMLSelectElement).value;
    this.instrumentChange.emit({
      ...this.instrument,
      type: 'continuous',
      collection,
      strategy: 'front_month',
    });
  }

  onSpecChange(next: TcgContinuousInstrumentRef): void {
    this.instrumentChange.emit(next);
  }
}
