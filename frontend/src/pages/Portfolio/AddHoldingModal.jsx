import { useCallback } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';

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
      if (instrument.type === 'continuous') {
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
    />
  );
}
