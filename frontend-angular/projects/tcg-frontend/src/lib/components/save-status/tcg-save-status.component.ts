import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

export type TcgSaveStatusValue = 'idle' | 'saving' | 'saved' | 'error';

const LABELS: Record<Exclude<TcgSaveStatusValue, 'idle'>, string> = {
  saving: 'saving…',
  saved: 'saved',
  error: 'save failed',
};

/**
 * Tiny inline indicator for backend autosave state. Mirrors React
 * `SaveStatus.jsx`: returns an empty template when `status === 'idle'`
 * (so the host UI doesn't reserve dead space) and renders an
 * `aria-live="polite"` badge otherwise. When status is `error` and a
 * detail message is supplied, the badge shows both an inline detail span
 * AND a native `title` tooltip (so screen readers and pointer users get
 * the same content).
 */
@Component({
  selector: 'tcg-save-status',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (status !== 'idle') {
      <span
        class="tcg-save-status"
        [class.tcg-save-status--saving]="status === 'saving'"
        [class.tcg-save-status--saved]="status === 'saved'"
        [class.tcg-save-status--error]="status === 'error'"
        [attr.data-status]="status"
        [attr.data-error-message]="status === 'error' && errorMessage ? errorMessage : null"
        role="status"
        aria-live="polite"
        [attr.title]="status === 'error' && errorMessage ? errorMessage : null"
      >
        {{ label }}: {{ statusLabel }}
        @if (status === 'error' && errorMessage) {
          <span class="tcg-save-status__detail"> — {{ errorMessage }}</span>
        }
      </span>
    }
  `,
  styles: [
    `
      .tcg-save-status {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.75rem;
      }
      .tcg-save-status--saving {
        background: var(--bg-hover, #f3f4f6);
        color: var(--text-secondary, #6b7280);
      }
      .tcg-save-status--saved {
        background: rgba(16, 185, 129, 0.12);
        color: #047857;
      }
      .tcg-save-status--error {
        background: rgba(239, 68, 68, 0.12);
        color: #b91c1c;
      }
      .tcg-save-status__detail {
        opacity: 0.8;
      }
    `,
  ],
})
export class TcgSaveStatusComponent {
  @Input({ required: true }) status!: TcgSaveStatusValue;
  @Input() label: string = 'Cloud';
  @Input() errorMessage: string | null = null;

  get statusLabel(): string {
    if (this.status === 'idle') return '';
    return LABELS[this.status];
  }
}
