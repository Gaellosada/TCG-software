import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
  computed,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { TcgSpotInstrumentRef } from './types';

export interface TcgSpotCandidate {
  symbol: string;
  collection: string;
}

/**
 * Spot leg picker — instrument typeahead. Mirrors React's `SpotLegPicker`:
 *   - tracks a local `query` string;
 *   - filters candidates by substring (case-insensitive), capped at 20;
 *   - emits a fully-populated `TcgSpotInstrumentRef` on selection;
 *   - clears the committed instrument while the user is mid-typing so
 *     an unconfirmed string never leaks as `instrument_id`.
 */
@Component({
  selector: 'tcg-spot-leg-picker',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tcg-slp" [attr.data-testid]="testId">
      <input
        type="text"
        class="tcg-slp__input"
        [value]="query()"
        (input)="onInput($event)"
        (focus)="showSuggestions.set(true)"
        (blur)="onBlur()"
        placeholder="Search instrument..."
        [attr.data-testid]="testId + '-instrument-input'"
      />
      @if (showSuggestions() && filtered().length > 0) {
        <ul class="tcg-slp__suggestions" [attr.data-testid]="testId + '-suggestions'">
          @for (c of filtered(); track c.collection + '|' + c.symbol) {
            <li
              class="tcg-slp__suggestion"
              role="button"
              tabindex="0"
              (mousedown)="onPick($event, c)"
              [attr.data-testid]="testId + '-suggestion-' + c.symbol"
            >
              <span>{{ c.symbol }}</span>
              <span class="tcg-slp__suggestion-coll">({{ c.collection }})</span>
            </li>
          }
        </ul>
      }
    </div>
  `,
  styles: [
    `
      .tcg-slp {
        position: relative;
        flex: 1;
      }
      .tcg-slp__input {
        width: 100%;
        padding: 4px 8px;
        background: var(--bg-surface, #fff);
        color: var(--text-primary, #1f2937);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
      }
      .tcg-slp__suggestions {
        list-style: none;
        margin: 0;
        padding: 0;
        position: absolute;
        top: 100%;
        left: 0;
        right: 0;
        max-height: 180px;
        overflow-y: auto;
        background: var(--bg-surface, #fff);
        border: 1px solid var(--border-primary, #d1d5db);
        border-radius: 4px;
        z-index: 1100;
      }
      .tcg-slp__suggestion {
        padding: 4px 8px;
        cursor: pointer;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.8rem;
      }
      .tcg-slp__suggestion-coll {
        margin-left: 8px;
        opacity: 0.6;
      }
    `,
  ],
})
export class TcgSpotLegPickerComponent {
  @Input({ required: true }) instrument!: TcgSpotInstrumentRef;
  @Input({ required: true }) candidateInstruments!: ReadonlyArray<TcgSpotCandidate>;
  @Input() testId: string = 'spot-leg';

  @Output() instrumentChange = new EventEmitter<TcgSpotInstrumentRef>();

  readonly query = signal<string>('');
  readonly showSuggestions = signal<boolean>(false);

  readonly filtered = computed(() => {
    const q = this.query().trim().toUpperCase();
    if (!q) return this.candidateInstruments.slice(0, 20);
    return this.candidateInstruments
      .filter((c) => c.symbol.toUpperCase().includes(q))
      .slice(0, 20);
  });

  ngOnChanges(): void {
    if (this.instrument?.instrument_id && this.query() !== this.instrument.instrument_id) {
      this.query.set(this.instrument.instrument_id);
    }
  }

  onInput(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
    this.showSuggestions.set(true);
    if (this.instrument?.instrument_id) {
      this.instrumentChange.emit({ type: 'spot', collection: '', instrument_id: '' });
    }
  }

  onBlur(): void {
    // Defer so onMouseDown on a suggestion has time to fire.
    setTimeout(() => this.showSuggestions.set(false), 120);
  }

  onPick(event: Event, c: TcgSpotCandidate): void {
    event.preventDefault();
    this.query.set(c.symbol);
    this.showSuggestions.set(false);
    this.instrumentChange.emit({ type: 'spot', collection: c.collection, instrument_id: c.symbol });
  }
}
