import { useCallback } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';
import { instrumentToLegConfig, legToInitialConfig } from './legConfig';

// A portfolio option leg is the option PRICE only (Issue #2 D1): restrict the
// option-stream picker to the PRICE series and hide iv/greeks/volume (those are
// signal-level operands, not portfolio legs). The three price streams are
// exposed so the Series selector RENDERS and the user can choose among them —
// CLOSE first (the faithful EOD settlement mark for a held-to-roll option; the
// default via the create-only heldInit one-shot), then mid / bs_mid. A NEW leg
// defaults to close; an EDITED leg keeps its persisted stream verbatim (heldInit
// is gated off in editMode). Module-level const so the array identity is stable.
const PORTFOLIO_OPTION_STREAMS = ['close', 'mid', 'bs_mid'];

/**
 * Portfolio-specific wrapper around InstrumentPickerModal, in two modes:
 *
 *   - ADD (default): translates a selection into a NEW leg (adds label +
 *     default weight) and appends via onAddLeg.
 *   - EDIT (when `editLeg` is non-null): pre-fills the picker with the leg's
 *     current config (inverse leg->modal translation) and, on confirm, UPDATES
 *     that leg in place via onUpdateLeg with the config fields only — the
 *     leg's id/label/weight are preserved (settings-only edit, no new leg).
 *
 * Props:
 *   isOpen        {boolean}
 *   onClose       {Function}  () => void
 *   onAddLeg      {Function}  (leg) => void            — ADD mode
 *   editLeg       {object|null}                        — non-null => EDIT mode
 *   onUpdateLeg   {Function}  (configUpdates) => void   — EDIT mode confirm
 *   readOnly      {boolean}   view-only (locked portfolio) — threads to the
 *                 picker; per the modal contract onSelect never fires when true.
 *   referenceDate {string|Date|null}  optional — the portfolio's start date,
 *                 forwarded to the option-leg implied-leverage readout as the
 *                 probe reference date (falls back to the root's last trade
 *                 date when null).
 */
export default function AddHoldingModal({
  isOpen,
  onClose,
  onAddLeg,
  editLeg = null,
  onUpdateLeg,
  readOnly = false,
  referenceDate = null,
}) {
  const editMode = editLeg != null;

  const handleSelect = useCallback(
    (instrument) => {
      const config = instrumentToLegConfig(instrument);
      if (editMode) {
        // Settings-only edit: merge the config fields over the existing leg.
        // updateLeg (usePortfolio) preserves id/label/weight and never appends.
        onUpdateLeg(config);
      } else {
        // Add: derive the display label (add-time only) and a default weight.
        let label;
        if (config.type === 'option_stream') {
          label = `${config.collection} ${config.option_type} ${config.stream}`;
        } else if (config.type === 'continuous') {
          label = config.collection;
        } else {
          label = config.symbol;
        }
        onAddLeg({ ...config, label, weight: 100 });
      }
      onClose();
    },
    [editMode, onAddLeg, onUpdateLeg, onClose],
  );

  return (
    <InstrumentPickerModal
      isOpen={isOpen}
      onClose={onClose}
      onSelect={handleSelect}
      title={editMode ? 'Edit Holding' : 'Add Holding'}
      // Non-null in edit mode => the picker opens pre-filled on the leg's
      // terminal config step (inverse leg->modal translation). null => create.
      initialConfig={editMode ? legToInitialConfig(editLeg) : null}
      readOnly={readOnly}
      optionStreamAllowedStreams={PORTFOLIO_OPTION_STREAMS}
      optionHoldRequired={true}
      optionReferenceDate={referenceDate}
    />
  );
}
