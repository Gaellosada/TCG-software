import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { TcgOptionStreamPickerComponent } from './tcg-option-stream-picker.component';
import { TcgOptionStreamRef } from './types';

/**
 * Option leg picker — wraps `<tcg-option-stream-picker>`. Mirrors React's
 * `OptionLegPicker`. The picker emits a full `TcgOptionStreamRef` shape;
 * partially-configured legs (no maturity / selection / stream) are
 * filtered out by the parent BasketComposer's `isInstrumentRefConfigured`
 * check before emit.
 */
@Component({
  selector: 'tcg-option-leg-picker',
  standalone: true,
  imports: [CommonModule, TcgOptionStreamPickerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tcg-olp" [attr.data-testid]="testId + '-option-leg'">
      <tcg-option-stream-picker
        [value]="streamValue"
        [availableRoots]="optionRoots"
        (valueChange)="instrumentChange.emit($event)"
        assetClass="option"
      ></tcg-option-stream-picker>
    </div>
  `,
  styles: [
    `
      .tcg-olp {
        flex: 1;
        display: flex;
        min-width: 0;
      }
    `,
  ],
})
export class TcgOptionLegPickerComponent {
  @Input({ required: true }) instrument!: TcgOptionStreamRef | { type: 'option_stream'; collection: string };
  @Input() optionRoots: ReadonlyArray<string> = [];
  @Input() testId: string = 'option-leg';

  @Output() instrumentChange = new EventEmitter<TcgOptionStreamRef>();

  get streamValue(): TcgOptionStreamRef | null {
    return this.instrument && (this.instrument as TcgOptionStreamRef).option_type
      ? (this.instrument as TcgOptionStreamRef)
      : null;
  }
}
