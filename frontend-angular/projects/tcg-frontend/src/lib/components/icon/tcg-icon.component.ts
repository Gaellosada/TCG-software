import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Inline SVG icon component. Mirrors the React `Icon.jsx` mapping name → SVG
 * primitives 1:1. Returns nothing when the requested name is unknown — same
 * behaviour as the React equivalent.
 *
 * Icons are inlined as static template fragments inside a single SVG host so
 * the consumer only ships the icons referenced by the runtime navigation.
 * Stroke/fill follow the host's `currentColor`.
 */
@Component({
  selector: 'tcg-icon',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg
      [attr.width]="size"
      [attr.height]="size"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      @switch (name) {
        @case ('data') {
          <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
          <path d="M21 12c0 1.66-4.03 3-9 3s-9-1.34-9-3"></path>
          <path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"></path>
        }
        @case ('portfolio') {
          <path d="M21.21 15.89A10 10 0 1 1 8 2.83"></path>
          <path d="M22 12A10 10 0 0 0 12 2v10z"></path>
        }
        @case ('help') {
          <circle cx="12" cy="12" r="10"></circle>
          <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path>
          <line x1="12" y1="17" x2="12.01" y2="17"></line>
        }
        @case ('settings') {
          <circle cx="12" cy="12" r="3"></circle>
          <path
            d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"
          ></path>
        }
        @case ('sun') {
          <circle cx="12" cy="12" r="5"></circle>
          <line x1="12" y1="1" x2="12" y2="3"></line>
          <line x1="12" y1="21" x2="12" y2="23"></line>
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
          <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
          <line x1="1" y1="12" x2="3" y2="12"></line>
          <line x1="21" y1="12" x2="23" y2="12"></line>
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
          <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
        }
        @case ('moon') {
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
        }
        @case ('indicators') {
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
        }
        @case ('signals') {
          <circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"></circle>
          <path d="M16.24 7.76a6 6 0 0 1 0 8.49"></path>
          <path d="M7.76 16.24a6 6 0 0 1 0-8.49"></path>
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14"></path>
          <path d="M4.93 19.07a10 10 0 0 1 0-14.14"></path>
        }
        @case ('chevron-down') {
          <polyline points="6 9 12 15 18 9"></polyline>
        }
        @case ('chevron-left') {
          <polyline points="15 18 9 12 15 6"></polyline>
        }
        @case ('chevron-right') {
          <polyline points="9 18 15 12 9 6"></polyline>
        }
        @case ('info') {
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="16" x2="12" y2="12"></line>
          <line x1="12" y1="8" x2="12.01" y2="8"></line>
        }
        @case ('ticket') {
          <path
            d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v2z"
          ></path>
          <line x1="13" y1="5" x2="13" y2="7"></line>
          <line x1="13" y1="11" x2="13" y2="13"></line>
          <line x1="13" y1="17" x2="13" y2="19"></line>
        }
      }
    </svg>
  `,
  styles: [
    `
      :host {
        display: inline-flex;
        line-height: 0;
      }
    `,
  ],
})
export class TcgIconComponent {
  /** Icon name. Returns nothing when unknown. */
  @Input({ required: true }) name!: string;
  /** Pixel size (square). */
  @Input() size: number = 18;
}
