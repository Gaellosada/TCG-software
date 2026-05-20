import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

/**
 * Compact header bar with a Save button + Auto save checkbox. Mirrors
 * React `SaveControls.jsx`:
 *   - Save button is disabled when not dirty or when `saveDisabled` is set;
 *   - Auto save checkbox flips via `autosaveChange`;
 *   - the `savedAtLabel` slot is rendered when `!dirty && savedAtLabel`;
 *   - the "Unsaved changes" hint appears when `dirty && !autosave`.
 *
 * The React `leftSlot` prop is replaced with `<ng-content
 * select="[tcg-save-controls-left]">` so consumers can project an
 * inline-name-input or any other custom widget into the leading row slot.
 */
@Component({
  selector: 'tcg-save-controls',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tcg-save-controls" [class]="className || ''" data-testid="save-controls">
      <div class="tcg-save-controls__left-slot">
        <ng-content select="[tcg-save-controls-left]"></ng-content>
      </div>
      <button
        type="button"
        class="tcg-save-controls__save-btn"
        (click)="save.emit()"
        [disabled]="disabled"
        aria-label="Save"
      >
        Save
      </button>
      <label class="tcg-save-controls__autosave-label">
        <input
          type="checkbox"
          [checked]="!!autosave"
          (change)="onAutosaveChange($event)"
          aria-label="Auto save"
        />
        Auto save
      </label>
      @if (!dirty && savedAtLabel) {
        <span class="tcg-save-controls__saved-at" aria-live="polite">{{ savedAtLabel }}</span>
      }
      @if (dirty && !autosave) {
        <span class="tcg-save-controls__unsaved" aria-live="polite">Unsaved changes</span>
      }
    </div>
  `,
  styles: [
    `
      .tcg-save-controls {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 12px;
        background: var(--bg-surface, #fff);
        border: 1px solid var(--border-primary, #e5e7eb);
        border-radius: 6px;
      }
      .tcg-save-controls__left-slot {
        flex: 1;
        min-width: 0;
      }
      .tcg-save-controls__save-btn {
        padding: 4px 14px;
        border-radius: 4px;
        background: var(--accent, #2563eb);
        color: #fff;
        border: none;
        font-size: 0.8125rem;
        cursor: pointer;
      }
      .tcg-save-controls__save-btn:disabled {
        background: var(--bg-disabled, #d1d5db);
        cursor: not-allowed;
      }
      .tcg-save-controls__autosave-label {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 0.8125rem;
        color: var(--text-secondary, #6b7280);
        cursor: pointer;
      }
      .tcg-save-controls__saved-at,
      .tcg-save-controls__unsaved {
        font-size: 0.75rem;
        color: var(--text-secondary, #6b7280);
      }
    `,
  ],
})
export class TcgSaveControlsComponent {
  @Input({ required: true }) dirty!: boolean;
  @Input({ required: true }) autosave!: boolean;
  @Input() savedAtLabel?: string;
  @Input() saveDisabled: boolean = false;
  @Input() className?: string;

  @Output() save = new EventEmitter<void>();
  @Output() autosaveChange = new EventEmitter<boolean>();

  get disabled(): boolean {
    return !this.dirty || this.saveDisabled;
  }

  onAutosaveChange(event: Event): void {
    const target = event.target as HTMLInputElement;
    this.autosaveChange.emit(target.checked);
  }
}
