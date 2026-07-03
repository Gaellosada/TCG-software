import { useCallback } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';

// A portfolio option leg is the option PRICE only (Issue #2 D1): pin the
// option-stream picker to mid and hide the Series selector. iv/greeks/volume
// are signal-level operands, not portfolio legs. Module-level const so the
// array identity is stable across renders (not recreated each render).
const PORTFOLIO_OPTION_STREAMS = ['mid'];

/**
 * Portfolio-specific wrapper around InstrumentPickerModal.
 * Translates the generic instrument selection into the portfolio leg format
 * (adds label, weight, maps type names).
 *
 * Props:
 *   isOpen   {boolean}
 *   onClose  {Function}  () => void
 *   onAddLeg {Function}  (leg) => void
 */
export default function AddHoldingModal({ isOpen, onClose, onAddLeg }) {
  const handleSelect = useCallback(
    (instrument) => {
      if (instrument.type === 'option_stream') {
        onAddLeg({
          label: `${instrument.collection} ${instrument.option_type} ${instrument.stream}`,
          type: 'option_stream',
          collection: instrument.collection,
          option_type: instrument.option_type,
          cycle: instrument.cycle,
          maturity: instrument.maturity,
          selection: instrument.selection,
          stream: instrument.stream,
          // Roll offset from OptionStreamForm — the unified {value, unit} object
          // (ROLL-EARLY axis), forwarded whole. Option streams carry no
          // back-adjustment, so there is no adjustment field (unlike continuous).
          // "Roll at end of month" is the EndOfMonth maturity, not a schedule.
          roll_offset: instrument.roll_offset,
          // PORTFOLIO option price legs are ALWAYS held (fixed-contract $-P&L);
          // the backend requires it, so force it on regardless of form state.
          hold_between_rolls: true,
          nav_times: instrument.nav_times ?? 1.0,
          weight: 100,
        });
      } else if (instrument.type === 'continuous') {
        onAddLeg({
          label: instrument.collection,
          type: 'continuous',
          collection: instrument.collection,
          strategy: instrument.strategy,
          adjustment: instrument.adjustment,
          cycle: instrument.cycle,
          rollOffset: instrument.rollOffset,
          weight: 100,
        });
      } else {
        onAddLeg({
          label: instrument.instrument_id,
          type: 'instrument',
          collection: instrument.collection,
          symbol: instrument.instrument_id,
          weight: 100,
        });
      }
      onClose();
    },
    [onAddLeg, onClose],
  );

  return (
    <InstrumentPickerModal
      isOpen={isOpen}
      onClose={onClose}
      onSelect={handleSelect}
      title="Add Holding"
      optionStreamAllowedStreams={PORTFOLIO_OPTION_STREAMS}
      optionHoldRequired={true}
    />
  );
}
