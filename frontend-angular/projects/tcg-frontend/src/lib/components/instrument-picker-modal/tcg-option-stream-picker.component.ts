import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { TcgOptionStreamRef } from './types';

const ALL_OPTION_TYPES: Array<'C' | 'P'> = ['C', 'P'];
const ALL_STREAMS = ['mid', 'iv', 'delta', 'gamma', 'vega', 'theta', 'open_interest', 'volume'];

/**
 * Option-stream picker. Mirrors React's `OptionStreamForm` at a minimal
 * surface — Phase A scaffold for the subsystem; full discriminated-union
 * UI (8 maturity kinds × 3 selection kinds) is ported in a later wave by
 * the worker that takes Signals/Indicators/Portfolio. The current shape
 * is enough for downstream callers to wire up + smoke; selection /
 * maturity sub-shapes are emitted verbatim from the bound `value`.
 *
 * REVIEW: scaffold — full form (defaults + validation + cycle list) is a
 * later-wave deliverable. The component still emits a valid
 * `TcgOptionStreamRef` shape so callers don't break.
 */
@Component({
  selector: 'tcg-option-stream-picker',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tcg-osp" data-testid="option-stream-picker">
      <label class="tcg-osp__field">
        <span>Root</span>
        <select [value]="value?.collection ?? ''" (change)="onRoot($event)">
          <option value="">— pick a root —</option>
          @for (r of availableRoots; track r) {
            <option [value]="r">{{ r }}</option>
          }
        </select>
      </label>
      <label class="tcg-osp__field">
        <span>Type</span>
        <select [value]="value?.option_type ?? 'C'" (change)="onType($event)">
          @for (t of types; track t) {
            <option [value]="t">{{ t }}</option>
          }
        </select>
      </label>
      <label class="tcg-osp__field">
        <span>Stream</span>
        <select [value]="value?.stream ?? 'mid'" (change)="onStream($event)">
          @for (s of streams; track s) {
            <option [value]="s">{{ s }}</option>
          }
        </select>
      </label>
    </div>
  `,
  styles: [
    `
      .tcg-osp {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }
      .tcg-osp__field {
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
    `,
  ],
})
export class TcgOptionStreamPickerComponent {
  @Input() value: TcgOptionStreamRef | null = null;
  @Input() availableRoots: ReadonlyArray<string> = [];
  /** Reserved for future per-class dispatch. */
  @Input() assetClass: 'option' = 'option';

  @Output() valueChange = new EventEmitter<TcgOptionStreamRef>();

  readonly types = ALL_OPTION_TYPES;
  readonly streams = ALL_STREAMS;

  private baseline(): TcgOptionStreamRef {
    return (
      this.value ?? {
        type: 'option_stream',
        collection: '',
        option_type: 'C',
        cycle: null,
        maturity: { kind: 'next_third_friday', offset_months: 0 },
        selection: { kind: 'by_moneyness', moneyness: 1.0 },
        stream: 'mid',
      }
    );
  }

  onRoot(event: Event): void {
    this.valueChange.emit({ ...this.baseline(), collection: (event.target as HTMLSelectElement).value });
  }
  onType(event: Event): void {
    const v = (event.target as HTMLSelectElement).value as 'C' | 'P';
    this.valueChange.emit({ ...this.baseline(), option_type: v });
  }
  onStream(event: Event): void {
    this.valueChange.emit({ ...this.baseline(), stream: (event.target as HTMLSelectElement).value });
  }
}
