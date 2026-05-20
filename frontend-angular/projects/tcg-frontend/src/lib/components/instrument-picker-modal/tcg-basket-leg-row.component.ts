import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { TcgContinuousLegPickerComponent } from './tcg-continuous-leg-picker.component';
import { TcgOptionLegPickerComponent } from './tcg-option-leg-picker.component';
import { TcgSpotCandidate, TcgSpotLegPickerComponent } from './tcg-spot-leg-picker.component';
import {
  TcgBasketAssetClass,
  TcgContinuousInstrumentRef,
  TcgInstrumentLeg,
  TcgOptionStreamRef,
  TcgSpotInstrumentRef,
} from './types';

/**
 * Per-leg row — dispatches the per-instrument renderer by `assetClass`.
 * Mirrors React's `BasketLegRow`. Weight + remove controls are common to
 * all three asset classes.
 */
@Component({
  selector: 'tcg-basket-leg-row',
  standalone: true,
  imports: [
    CommonModule,
    TcgSpotLegPickerComponent,
    TcgContinuousLegPickerComponent,
    TcgOptionLegPickerComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="tcg-blr"
      [class.tcg-blr--option]="instrument.type === 'option_stream'"
      [attr.data-testid]="testId"
      [attr.data-asset-class]="assetClass"
      [attr.data-instrument-type]="instrument.type || ''"
    >
      @switch (instrument.type) {
        @case ('spot') {
          <tcg-spot-leg-picker
            [instrument]="$any(instrument)"
            [candidateInstruments]="candidateInstruments"
            [testId]="testId"
            (instrumentChange)="onInstrumentChange($event)"
          ></tcg-spot-leg-picker>
        }
        @case ('continuous') {
          <tcg-continuous-leg-picker
            [instrument]="$any(instrument)"
            [candidateCollections]="candidateCollections"
            [testId]="testId"
            (instrumentChange)="onInstrumentChange($event)"
          ></tcg-continuous-leg-picker>
        }
        @case ('option_stream') {
          <tcg-option-leg-picker
            [instrument]="$any(instrument)"
            [optionRoots]="optionRoots"
            [testId]="testId"
            (instrumentChange)="onInstrumentChange($event)"
          ></tcg-option-leg-picker>
        }
        @default {
          <div [attr.data-testid]="testId + '-unknown-type'">Unsupported asset class</div>
        }
      }
      <input
        type="number"
        step="any"
        class="tcg-blr__weight"
        [value]="Number.isFinite(weight) ? weight : ''"
        (input)="onWeight($event)"
        placeholder="±1.0"
        [attr.data-testid]="testId + '-weight-input'"
        [attr.aria-invalid]="!weightValid"
      />
      <button
        type="button"
        class="tcg-blr__remove"
        (click)="remove.emit()"
        aria-label="Remove leg"
        [attr.data-testid]="testId + '-remove'"
      >
        &times;
      </button>
    </div>
  `,
  styles: [
    `
      .tcg-blr {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
        background: var(--bg-primary, #f9fafb);
      }
      .tcg-blr--option {
        align-items: flex-start;
      }
      .tcg-blr__weight {
        width: 80px;
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
      }
      .tcg-blr__remove {
        background: transparent;
        border: none;
        font-size: 1rem;
        cursor: pointer;
        color: var(--text-secondary, #6b7280);
      }
    `,
  ],
})
export class TcgBasketLegRowComponent {
  @Input({ required: true }) instrument!: TcgInstrumentLeg;
  @Input({ required: true }) weight!: number;
  @Input({ required: true }) assetClass!: TcgBasketAssetClass;
  @Input() candidateInstruments: ReadonlyArray<TcgSpotCandidate> = [];
  @Input() candidateCollections: ReadonlyArray<string> = [];
  @Input() optionRoots: ReadonlyArray<string> = [];
  @Input() testId: string = 'basket-leg';

  @Output() instrumentChange = new EventEmitter<TcgInstrumentLeg>();
  @Output() weightChange = new EventEmitter<number>();
  @Output() remove = new EventEmitter<void>();

  // Expose `Number` to the template so we can call `Number.isFinite`.
  readonly Number = Number;

  get weightValid(): boolean {
    return Number.isFinite(this.weight) && this.weight !== 0;
  }

  onInstrumentChange(
    next: TcgSpotInstrumentRef | TcgContinuousInstrumentRef | TcgOptionStreamRef,
  ): void {
    this.instrumentChange.emit(next);
  }

  onWeight(event: Event): void {
    const raw = (event.target as HTMLInputElement).value;
    if (raw === '' || raw === '-') {
      this.weightChange.emit(NaN);
      return;
    }
    const parsed = parseFloat(raw);
    this.weightChange.emit(Number.isFinite(parsed) ? parsed : NaN);
  }
}
