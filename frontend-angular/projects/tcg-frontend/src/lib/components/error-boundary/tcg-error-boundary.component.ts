import {
  ChangeDetectionStrategy,
  Component,
  ErrorHandler,
  Injectable,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Component-scoped ErrorHandler that captures async / template errors raised
 * inside its provider tree and surfaces them to the surrounding
 * `TcgErrorBoundaryComponent`. Angular's idiomatic equivalent of React's
 * `componentDidCatch` is `ErrorHandler.handleError`; we expose the latest
 * error as a signal the boundary template can react to.
 *
 * Component-scoped (G5): provided on `TcgErrorBoundaryComponent` itself
 * via `providers: [...]`, NOT root.
 */
@Injectable()
export class TcgErrorBoundaryHandler implements ErrorHandler {
  readonly error = signal<Error | null>(null);

  handleError(err: unknown): void {
    const error = err instanceof Error ? err : new Error(String(err));
    // Mirror React boundary's `console.error('[ErrorBoundary]', error)`.
    // eslint-disable-next-line no-console
    console.error('[TcgErrorBoundary]', error);
    this.error.set(error);
  }

  reset(): void {
    this.error.set(null);
  }
}

/**
 * Generic error boundary — catches render / async errors raised inside the
 * projected content and renders a recoverable fallback panel instead.
 * Mirrors React's `<ErrorBoundary>` API (custom fallback via projected
 * `[tcg-error-boundary-fallback]` slot, default fallback otherwise).
 *
 * Note: Angular templates don't bubble template errors the same way React
 * does, so this boundary catches via a scoped `ErrorHandler`. It still
 * gives consumers a Retry button that re-renders the content.
 */
@Component({
  selector: 'tcg-error-boundary',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    TcgErrorBoundaryHandler,
    { provide: ErrorHandler, useExisting: TcgErrorBoundaryHandler },
  ],
  template: `
    @if (handler.error()) {
      <ng-content select="[tcg-error-boundary-fallback]"></ng-content>
      <ng-container *ngIf="!hasFallback">
        <div class="tcg-error-boundary">
          <strong class="tcg-error-boundary__title">Something went wrong</strong>
          <p class="tcg-error-boundary__message">
            {{ handler.error()?.message || 'Unexpected rendering error' }}
          </p>
          <button
            type="button"
            class="tcg-error-boundary__retry"
            (click)="handler.reset()"
          >
            Retry
          </button>
        </div>
      </ng-container>
    } @else {
      <ng-content></ng-content>
    }
  `,
  styles: [
    `
      .tcg-error-boundary {
        padding: 24px;
        margin: 16px;
        border-radius: 8px;
        border: 1px solid var(--border-primary, #e5e7eb);
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
      }
      .tcg-error-boundary__title {
        display: block;
        margin-bottom: 8px;
      }
      .tcg-error-boundary__message {
        margin: 0 0 12px;
        color: var(--text-secondary, #6b7280);
        font-size: 0.875rem;
      }
      .tcg-error-boundary__retry {
        padding: 6px 16px;
        border-radius: 6px;
        border: 1px solid var(--border-primary, #d1d5db);
        background: var(--bg-primary, #f9fafb);
        color: var(--text-primary, #1f2937);
        cursor: pointer;
        font-size: 0.8125rem;
      }
    `,
  ],
})
export class TcgErrorBoundaryComponent {
  readonly handler = inject(TcgErrorBoundaryHandler);

  /**
   * Consumers can provide their own fallback via the
   * `[tcg-error-boundary-fallback]` projection slot. We default to
   * rendering the built-in panel when no custom slot content is supplied —
   * REVIEW: relies on the consumer not projecting an empty slot.
   */
  hasFallback = false;
}
