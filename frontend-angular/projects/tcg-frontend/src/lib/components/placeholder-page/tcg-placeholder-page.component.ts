import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Generic placeholder page — used by Tickets / RunningSignals / MongoDBAgent
 * routes (and any future route that hasn't been ported yet). Mirrors React's
 * `PlaceholderPage.jsx`: a title + a description (with the same default
 * copy).
 */
@Component({
  selector: 'tcg-placeholder-page',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tcg-placeholder-page">
      <h1 class="tcg-placeholder-page__title">{{ title }}</h1>
      <p class="tcg-placeholder-page__description">{{ resolvedDescription }}</p>
    </div>
  `,
  styles: [
    `
      .tcg-placeholder-page {
        padding: 32px;
      }
      .tcg-placeholder-page__title {
        font-size: 1.4rem;
        margin: 0 0 8px;
        color: var(--text-primary, #1f2937);
      }
      .tcg-placeholder-page__description {
        margin: 0;
        color: var(--text-secondary, #6b7280);
        font-size: 0.9rem;
      }
    `,
  ],
})
export class TcgPlaceholderPageComponent {
  @Input({ required: true }) title!: string;
  @Input() description?: string;

  get resolvedDescription(): string {
    return this.description ?? 'This page is incoming work. Check back soon.';
  }
}
