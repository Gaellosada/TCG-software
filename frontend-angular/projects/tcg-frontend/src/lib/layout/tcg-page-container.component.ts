import { ChangeDetectionStrategy, Component } from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Thin page-shell wrapper. Mirrors React's `PageContainer.jsx`: provides
 * default page padding + max-width via projected `<ng-content>`. Used by
 * every page route to share a consistent page chrome.
 */
@Component({
  selector: 'tcg-page-container',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: ` <div class="tcg-page-container"><ng-content></ng-content></div> `,
  styles: [
    `
      .tcg-page-container {
        padding: 16px 24px;
        max-width: 1400px;
        margin: 0 auto;
        box-sizing: border-box;
      }
    `,
  ],
})
export class TcgPageContainerComponent {}
