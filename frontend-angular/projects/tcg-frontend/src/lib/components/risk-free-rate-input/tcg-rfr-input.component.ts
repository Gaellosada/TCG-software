import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

/**
 * Risk-free-rate input. Mirrors React `RiskFreeRateInput.jsx`:
 *   - displays a numeric percent input with step 0.01 and a trailing `%`
 *     unit suffix;
 *   - emits the raw string the user typed via `valuePctChange` so the
 *     consumer can validate / store as it pleases (matches the React
 *     `onChange` shape which forwarded the `Event`).
 *
 * Unit boundary: the on-screen value is a percent string (e.g. `"4.00"`).
 * The conversion to a fraction lives in `TcgUserSettingsService`.
 */
@Component({
  selector: 'tcg-rfr-input',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <label class="tcg-rfr-input" [class]="className || ''">
      @if (label) {
        <span class="tcg-rfr-input__label">{{ label }}</span>
      }
      <input
        type="number"
        step="0.01"
        min="0"
        class="tcg-rfr-input__field"
        [value]="valuePct"
        (input)="onInput($event)"
        [attr.aria-label]="ariaLabel || null"
      />
      <span class="tcg-rfr-input__unit">%</span>
    </label>
  `,
  styles: [
    `
      .tcg-rfr-input {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.875rem;
        color: var(--text-primary, #1f2937);
      }
      .tcg-rfr-input__field {
        width: 80px;
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
        font-size: 0.875rem;
      }
      .tcg-rfr-input__unit {
        color: var(--text-secondary, #6b7280);
      }
    `,
  ],
})
export class TcgRfrInputComponent {
  @Input({ required: true }) valuePct!: string;
  @Input() ariaLabel?: string;
  @Input() label?: string;
  @Input() className?: string;

  @Output() valuePctChange = new EventEmitter<string>();

  onInput(event: Event): void {
    const target = event.target as HTMLInputElement;
    this.valuePctChange.emit(target.value);
  }
}
