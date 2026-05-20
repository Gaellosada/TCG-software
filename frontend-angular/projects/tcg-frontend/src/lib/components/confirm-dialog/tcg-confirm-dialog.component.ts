import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  Input,
  Output,
  ViewChild,
  afterNextRender,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { A11yModule } from '@angular/cdk/a11y';

/**
 * Reusable modal confirmation dialog. Mirrors the React `ConfirmDialog`:
 *   - rendered conditionally on `open()`;
 *   - auto-focuses the confirm button (deferred to next render via
 *     `afterNextRender`);
 *   - Escape ⇒ cancel; Enter ⇒ confirm;
 *   - backdrop click ⇒ cancel;
 *   - focus trap: handled by CDK's `cdkTrapFocus` directive rather than
 *     the React hand-rolled Tab handler.
 *
 * NOTE: this implementation renders inline within its host's DOM tree
 * (with a high `z-index`) rather than via CDK Overlay portaling — that
 * avoids consumers needing to add `OverlayModule` and matches the React
 * `createPortal(document.body)` semantics closely enough for a confirm
 * dialog. If z-index stacking becomes an issue, swap to CDK Overlay.
 */
@Component({
  selector: 'tcg-confirm-dialog',
  standalone: true,
  imports: [CommonModule, A11yModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (open) {
      <div
        class="tcg-confirm-dialog__backdrop"
        (mousedown)="onBackdropMouseDown($event)"
        data-testid="confirm-dialog-backdrop"
      >
        <div
          class="tcg-confirm-dialog__card"
          role="dialog"
          aria-modal="true"
          aria-labelledby="tcg-confirm-dialog-title"
          data-testid="confirm-dialog"
          cdkTrapFocus
          [cdkTrapFocusAutoCapture]="true"
          (keydown.escape)="onEscape($event)"
          (keydown.enter)="onEnter($event)"
        >
          @if (title) {
            <h3 id="tcg-confirm-dialog-title" class="tcg-confirm-dialog__title">{{ title }}</h3>
          }
          @if (message) {
            <div class="tcg-confirm-dialog__message">{{ message }}</div>
          }
          <div class="tcg-confirm-dialog__actions">
            <button
              #cancelBtn
              type="button"
              class="tcg-confirm-dialog__cancel-btn"
              (click)="cancel.emit()"
              data-testid="confirm-dialog-cancel"
            >
              {{ cancelLabel }}
            </button>
            <button
              #confirmBtn
              type="button"
              class="tcg-confirm-dialog__confirm-btn"
              [class.tcg-confirm-dialog__confirm-btn--destructive]="destructive"
              (click)="confirm.emit()"
              data-testid="confirm-dialog-confirm"
            >
              {{ confirmLabel }}
            </button>
          </div>
        </div>
      </div>
    }
  `,
  styles: [
    `
      .tcg-confirm-dialog__backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
      }
      .tcg-confirm-dialog__card {
        background: var(--bg-surface, #fff);
        border-radius: 8px;
        padding: 20px;
        max-width: 480px;
        width: 90%;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
      }
      .tcg-confirm-dialog__title {
        margin: 0 0 12px;
        font-size: 1rem;
      }
      .tcg-confirm-dialog__message {
        font-size: 0.875rem;
        color: var(--text-secondary, #6b7280);
        margin-bottom: 16px;
      }
      .tcg-confirm-dialog__actions {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
      }
      .tcg-confirm-dialog__cancel-btn,
      .tcg-confirm-dialog__confirm-btn {
        padding: 6px 16px;
        border-radius: 6px;
        font-size: 0.8125rem;
        cursor: pointer;
        border: 1px solid var(--border-primary, #d1d5db);
      }
      .tcg-confirm-dialog__cancel-btn {
        background: var(--bg-primary, #f9fafb);
        color: var(--text-primary, #1f2937);
      }
      .tcg-confirm-dialog__confirm-btn {
        background: var(--accent, #2563eb);
        color: #fff;
        border: none;
      }
      .tcg-confirm-dialog__confirm-btn--destructive {
        background: #dc2626;
      }
    `,
  ],
})
export class TcgConfirmDialogComponent {
  @Input({ required: true }) open!: boolean;
  @Input() title?: string;
  @Input() message?: string;
  @Input() confirmLabel: string = 'Confirm';
  @Input() cancelLabel: string = 'Cancel';
  @Input() destructive: boolean = false;

  @Output() confirm = new EventEmitter<void>();
  @Output() cancel = new EventEmitter<void>();

  @ViewChild('confirmBtn') confirmBtn?: ElementRef<HTMLButtonElement>;

  private readonly host = inject(ElementRef<HTMLElement>);

  constructor() {
    // Defer focus to next render so the DOM node is mounted before .focus().
    afterNextRender(() => {
      if (this.open && this.confirmBtn) {
        this.confirmBtn.nativeElement.focus();
      }
    });
  }

  onBackdropMouseDown(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.cancel.emit();
    }
  }

  onEscape(event: Event): void {
    event.preventDefault();
    this.cancel.emit();
  }

  onEnter(event: Event): void {
    event.preventDefault();
    this.confirm.emit();
  }
}
