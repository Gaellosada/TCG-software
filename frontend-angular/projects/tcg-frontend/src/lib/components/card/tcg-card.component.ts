import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Shared titled card / section panel. Mirrors the React `Card.jsx` shape:
 * an optional header (title on the left, projected actions on the right)
 * followed by a body slot that takes whatever content the consumer projects.
 *
 * Projected slots use `<ng-content select="[tcg-card-actions]">` for the
 * header-right actions and the default slot for the body — matches the
 * React `right` + `children` prop split.
 */
@Component({
  selector: 'tcg-card',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (title || hasActions) {
      <div class="tcg-card__header">
        @if (title) {
          <span class="tcg-card__title">{{ title }}</span>
        }
        <span class="tcg-card__actions">
          <ng-content select="[tcg-card-actions]"></ng-content>
        </span>
      </div>
    }
    <div class="tcg-card__body">
      <ng-content></ng-content>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        background: var(--bg-surface, #fff);
        border: 1px solid var(--border-primary, #e5e7eb);
        border-radius: var(--radius-md, 8px);
        overflow: hidden;
      }
      .tcg-card__header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border-bottom: 1px solid var(--border-primary, #e5e7eb);
      }
      .tcg-card__title {
        font-weight: 600;
        font-size: 0.95rem;
        color: var(--text-primary, #1f2937);
      }
      .tcg-card__actions {
        display: inline-flex;
        gap: 8px;
        align-items: center;
      }
      .tcg-card__body {
        padding: 12px 16px;
      }
    `,
  ],
})
export class TcgCardComponent {
  @Input() title?: string;

  /** True when the actions slot has any projected content. */
  hasActions = true;
}
