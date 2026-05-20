import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
  computed,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { TcgIconComponent } from '../components/icon/tcg-icon.component';
import { TCG_NAV_SECTIONS, TcgNavSection } from './nav-config';

/**
 * Collapsible router sidebar. Mirrors React's `Sidebar.jsx`:
 *   - reads sections from the locked `TCG_NAV_SECTIONS` const;
 *   - first section with `anchor: 'bottom'` is given `margin-top: auto`
 *     so the App-section sticks to the bottom of the sidebar;
 *   - `routerLinkActive` replaces React's `<NavLink>` active class;
 *   - emits `(toggled)` so the host can dispatch a resize event for
 *     Plotly (the React app fires `window.dispatchEvent(new Event('resize'))`
 *     ~260ms after toggling — same hack documented in the Wave R risk #9).
 */
@Component({
  selector: 'tcg-sidebar',
  standalone: true,
  imports: [CommonModule, RouterLink, RouterLinkActive, TcgIconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <aside class="tcg-sidebar" [class.tcg-sidebar--collapsed]="collapsed">
      <div class="tcg-sidebar__logo">
        @if (!collapsed) {
          <span class="tcg-sidebar__logo-text">TCG</span>
        }
        <button
          type="button"
          class="tcg-sidebar__toggle"
          (click)="onToggle()"
          [attr.title]="collapsed ? 'Expand sidebar' : 'Collapse sidebar'"
          [attr.aria-expanded]="!collapsed"
          [attr.aria-label]="collapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        >
          <tcg-icon [name]="collapsed ? 'chevron-right' : 'chevron-left'" [size]="16"></tcg-icon>
        </button>
      </div>
      @for (section of sections; track section.id; let idx = $index) {
        <div
          class="tcg-sidebar__section"
          [class.tcg-sidebar__section--bottom]="idx === firstBottomIdx()"
          [attr.data-section-id]="section.id"
        >
          @if (idx > 0) {
            <div class="tcg-sidebar__section-divider"></div>
          }
          @if (!collapsed) {
            <span class="tcg-sidebar__section-label">{{ section.label }}</span>
          }
          <nav>
            <ul class="tcg-sidebar__nav-list">
              @for (item of section.items; track item.path) {
                <li class="tcg-sidebar__nav-item">
                  <a
                    [routerLink]="item.path"
                    routerLinkActive="tcg-sidebar__nav-link--active"
                    class="tcg-sidebar__nav-link"
                    [attr.title]="collapsed ? item.label : null"
                  >
                    <span class="tcg-sidebar__nav-icon">
                      <tcg-icon [name]="item.icon" [size]="18"></tcg-icon>
                    </span>
                    @if (!collapsed) {
                      <span class="tcg-sidebar__nav-label">{{ item.label }}</span>
                    }
                  </a>
                </li>
              }
            </ul>
          </nav>
        </div>
      }
    </aside>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }
      .tcg-sidebar {
        display: flex;
        flex-direction: column;
        width: 220px;
        height: 100%;
        background: var(--bg-surface, #1f2937);
        color: var(--text-on-surface, #e5e7eb);
        border-right: 1px solid var(--border-primary, #374151);
        transition: width 0.2s ease;
      }
      .tcg-sidebar--collapsed {
        width: 56px;
      }
      .tcg-sidebar__logo {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px;
        border-bottom: 1px solid var(--border-primary, #374151);
      }
      .tcg-sidebar__logo-text {
        font-weight: 700;
        font-size: 1.1rem;
      }
      .tcg-sidebar__toggle {
        background: transparent;
        border: none;
        color: inherit;
        cursor: pointer;
        padding: 4px;
      }
      .tcg-sidebar__section {
        padding: 8px 0;
      }
      .tcg-sidebar__section--bottom {
        margin-top: auto;
      }
      .tcg-sidebar__section-divider {
        height: 1px;
        background: var(--border-primary, #374151);
        margin: 4px 12px;
      }
      .tcg-sidebar__section-label {
        display: block;
        padding: 4px 12px;
        font-size: 0.7rem;
        text-transform: uppercase;
        opacity: 0.6;
        letter-spacing: 0.05em;
      }
      .tcg-sidebar__nav-list {
        list-style: none;
        padding: 0;
        margin: 0;
      }
      .tcg-sidebar__nav-link {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 12px;
        color: inherit;
        text-decoration: none;
        font-size: 0.875rem;
        border-radius: 4px;
      }
      .tcg-sidebar__nav-link:hover {
        background: var(--bg-hover, rgba(255, 255, 255, 0.05));
      }
      .tcg-sidebar__nav-link--active {
        background: var(--accent, #2563eb);
        color: #fff;
      }
      .tcg-sidebar__nav-icon {
        display: inline-flex;
      }
    `,
  ],
})
export class TcgSidebarComponent {
  @Input() collapsed: boolean = false;
  @Output() toggled = new EventEmitter<boolean>();

  readonly sections: ReadonlyArray<TcgNavSection> = TCG_NAV_SECTIONS;
  readonly firstBottomIdx = computed(() =>
    this.sections.findIndex((s) => s.anchor === 'bottom'),
  );

  onToggle(): void {
    this.toggled.emit(!this.collapsed);
  }
}
