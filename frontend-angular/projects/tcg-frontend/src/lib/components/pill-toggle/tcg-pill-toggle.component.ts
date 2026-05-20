import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';

export interface TcgPillOption {
  value: string;
  label: string;
}

/**
 * Pill-style toggle button group. Mirrors React `PillToggle.jsx`:
 *   - options as `{value, label}` pairs;
 *   - active value highlighted via the `tcg-pill-toggle__btn--active` class;
 *   - clicking a non-active option emits the new value.
 *
 * `aria-pressed` / `aria-label` mirror the React semantics for keyboard +
 * screen-reader users.
 */
@Component({
  selector: 'tcg-pill-toggle',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="tcg-pill-toggle"
      role="group"
      [attr.aria-label]="ariaLabel || null"
      [attr.title]="tooltip || null"
    >
      @for (opt of options; track opt.value) {
        <button
          type="button"
          class="tcg-pill-toggle__btn"
          [class.tcg-pill-toggle__btn--active]="opt.value === value"
          (click)="onClick(opt.value)"
          [attr.aria-pressed]="opt.value === value"
        >
          {{ opt.label }}
        </button>
      }
    </div>
  `,
  styles: [
    `
      .tcg-pill-toggle {
        display: inline-flex;
        background: var(--bg-primary, #f3f4f6);
        border: 1px solid var(--border-primary, #e5e7eb);
        border-radius: 999px;
        padding: 2px;
      }
      .tcg-pill-toggle__btn {
        background: transparent;
        border: none;
        color: var(--text-secondary, #6b7280);
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 0.8125rem;
        cursor: pointer;
      }
      .tcg-pill-toggle__btn--active {
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
      }
    `,
  ],
})
export class TcgPillToggleComponent {
  @Input({ required: true }) options!: ReadonlyArray<TcgPillOption>;
  @Input({ required: true }) value!: string;
  @Input() ariaLabel?: string;
  @Input() tooltip?: string;

  @Output() valueChange = new EventEmitter<string>();

  onClick(next: string): void {
    if (next !== this.value) this.valueChange.emit(next);
  }
}
