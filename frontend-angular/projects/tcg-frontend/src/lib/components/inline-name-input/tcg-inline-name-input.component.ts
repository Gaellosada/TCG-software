import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

export interface TcgRenamableEntity {
  id: string;
  name: string;
  readonly?: boolean;
}

/**
 * Inline name input. Mirrors React `InlineNameInput.jsx`:
 *   - tracks a local draft so typing does not re-render the host page each
 *     keystroke;
 *   - commits on blur or Enter;
 *   - when the bound entity changes (different `id`), resets the draft from
 *     the new entity's name;
 *   - while the user is focused on the input, ignores external `name`
 *     mutations (the React hook tracked this via a focus ref — we mirror
 *     it with a private `focused` flag).
 */
@Component({
  selector: 'tcg-inline-name-input',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <input
      type="text"
      [class]="className || ''"
      [value]="draft()"
      [disabled]="readonly"
      [placeholder]="entity ? (selectedPlaceholder || '') : (placeholder || '')"
      [attr.aria-label]="ariaLabel || null"
      [attr.title]="resolvedTitle ?? null"
      (input)="onInput($event)"
      (focus)="onFocus()"
      (blur)="onBlur()"
      (keydown.enter)="onEnter($event)"
    />
  `,
  styles: [
    `
      :host {
        display: inline-flex;
        width: 100%;
      }
      input {
        width: 100%;
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
        font-size: 0.875rem;
      }
      input:disabled {
        background: var(--bg-primary, #f3f4f6);
        color: var(--text-secondary, #6b7280);
      }
    `,
  ],
})
export class TcgInlineNameInputComponent implements OnChanges {
  @Input() entity: TcgRenamableEntity | null = null;
  @Input() className?: string;
  @Input() placeholder?: string;
  @Input() selectedPlaceholder?: string;
  @Input() ariaLabel?: string;
  /** Either a string or a resolver function — mirrors the React API. */
  @Input() title?: string | ((entity: TcgRenamableEntity | null) => string | undefined);

  @Output() rename = new EventEmitter<{ id: string; name: string }>();

  readonly draft = signal<string>('');
  private focused = false;
  private prevId: string | null = null;

  get readonly(): boolean {
    return !this.entity || !!this.entity.readonly;
  }

  get resolvedTitle(): string | undefined {
    if (typeof this.title === 'function') return this.title(this.entity);
    return this.title;
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['entity']) {
      const e = this.entity;
      const id = e?.id ?? null;
      if (this.prevId !== id) {
        this.prevId = id;
        this.draft.set(e?.name || '');
      } else if (!this.focused && (e?.name || '') !== this.draft()) {
        this.draft.set(e?.name || '');
      }
    }
  }

  onInput(event: Event): void {
    const target = event.target as HTMLInputElement;
    this.draft.set(target.value);
  }

  onFocus(): void {
    this.focused = true;
  }

  onBlur(): void {
    this.focused = false;
    this.commit();
  }

  onEnter(event: Event): void {
    event.preventDefault();
    (event.target as HTMLInputElement).blur();
  }

  private commit(): void {
    const e = this.entity;
    if (!e || this.readonly) {
      this.draft.set(e?.name || '');
      return;
    }
    const next = this.draft().trim();
    if (!next || next === e.name) {
      this.draft.set(e.name);
      return;
    }
    this.rename.emit({ id: e.id, name: next });
  }
}
