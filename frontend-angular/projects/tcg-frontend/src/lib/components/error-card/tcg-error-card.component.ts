import { ChangeDetectionStrategy, Component, Input, signal } from '@angular/core';
import { CommonModule } from '@angular/common';

export interface TcgErrorEnvelope {
  error_type: string;
  message: string;
  traceback?: string;
}

/**
 * Shared error card for structured error envelopes:
 *   `{ error_type, message, traceback? }`.
 *
 * Mirrors the React `ErrorCard.jsx` semantics:
 *   - `headings` maps `error_type` → heading text;
 *   - `fallbackHeading` covers unmatched types;
 *   - optional `icons` map renders an inline SVG icon next to the heading;
 *   - `coerceErrorType` (optional) lets callers normalise `error_type` so
 *     `data-error-type` reflects the resolved bucket (Indicators maps
 *     unknown types to `'generic'`; Signals renders the raw value).
 *
 * The React side passed a CSS-modules `styles` object so each call-site
 * could keep its own visual design — Angular's `ViewEncapsulation.Emulated`
 * solves that automatically. We expose `className` so consumers can theme
 * the host without forking the component.
 */
@Component({
  selector: 'tcg-error-card',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="tcg-error-card"
      [class]="className || ''"
      [attr.data-error-type]="resolvedKind"
      role="alert"
    >
      <div class="tcg-error-card__header">
        @if (iconPath) {
          <svg
            viewBox="0 0 24 24"
            class="tcg-error-card__icon"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            width="22"
            height="22"
            aria-hidden="true"
          >
            <path [attr.d]="iconPath"></path>
          </svg>
        }
        <h3 class="tcg-error-card__heading">{{ heading }}</h3>
        <button
          type="button"
          class="tcg-error-card__copy-btn"
          (click)="onCopy()"
          aria-label="Copy error details"
        >
          {{ copied() ? 'Copied!' : 'Copy' }}
        </button>
      </div>
      <p class="tcg-error-card__message">{{ error.message }}</p>
      @if (error.traceback) {
        <details class="tcg-error-card__traceback-details">
          <summary>Show traceback</summary>
          <pre class="tcg-error-card__traceback-pre">{{ error.traceback }}</pre>
        </details>
      }
    </div>
  `,
  styles: [
    `
      .tcg-error-card {
        padding: 16px;
        border: 1px solid #fca5a5;
        background: #fee2e2;
        color: #991b1b;
        border-radius: 8px;
      }
      .tcg-error-card__header {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .tcg-error-card__heading {
        margin: 0;
        font-size: 0.95rem;
        flex: 1;
      }
      .tcg-error-card__copy-btn {
        padding: 4px 10px;
        font-size: 0.75rem;
        background: #fff;
        border: 1px solid #fca5a5;
        border-radius: 4px;
        color: inherit;
        cursor: pointer;
      }
      .tcg-error-card__message {
        margin: 8px 0 0;
        font-size: 0.85rem;
      }
      .tcg-error-card__traceback-pre {
        background: #fff;
        padding: 8px;
        border-radius: 4px;
        overflow-x: auto;
        font-size: 0.75rem;
      }
    `,
  ],
})
export class TcgErrorCardComponent {
  @Input({ required: true }) error!: TcgErrorEnvelope;
  @Input({ required: true }) headings!: Record<string, string>;
  @Input({ required: true }) fallbackHeading!: string;
  @Input() icons?: Record<string, string>;
  @Input() className?: string;
  @Input() coerceErrorType?: (errorType: string, headings: Record<string, string>) => string;

  readonly copied = signal(false);

  get resolvedKind(): string {
    if (this.coerceErrorType) return this.coerceErrorType(this.error.error_type, this.headings);
    return this.error.error_type;
  }

  get heading(): string {
    return this.headings[this.resolvedKind] || this.fallbackHeading;
  }

  get iconPath(): string | null {
    if (!this.icons) return null;
    return this.icons[this.resolvedKind] ?? null;
  }

  onCopy(): void {
    const blob = this.error.traceback
      ? `${this.error.error_type}: ${this.error.message}\n\n${this.error.traceback}`
      : this.error.message;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(blob).then(
          () => {
            this.copied.set(true);
            setTimeout(() => this.copied.set(false), 1600);
          },
          () => {
            /* clipboard blocked — swallow silently */
          },
        );
      }
    } catch {
      /* ignore */
    }
  }
}
