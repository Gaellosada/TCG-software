import { useCallback, useEffect, useRef, useState } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';
import { listSignals, describePersistenceError } from '../../api/persistence';
import { hydrateFromPersisted } from '../Signals/hydrateSignal';
import { formatInstrument } from './formatInstrument';
import styles from './SignalPickerModal.module.css';

// Categories selectable in this picker. Mirrors the Signals page selector
// but excludes ARCHIVE — you don't add archived (soft-deleted) signals to a
// portfolio. Default matches SignalsPage: RESEARCH.
const PICKER_CATEGORIES = /** @type {const} */ (['RESEARCH', 'DEV', 'PROD']);
const DEFAULT_CATEGORY = 'RESEARCH';

/**
 * Two-step modal for adding a signal as a portfolio holding.
 *
 * Step 1: Pick a signal from the backend-persisted signals list. A category
 *         selector (RESEARCH / DEV / PROD) filters which signals are shown;
 *         switching it refetches. Four distinct states are rendered:
 *         loading, error (with retry), empty, and the signal list.
 * Step 2: Configure inputs — pre-filled from the saved signal, editable via
 *         InstrumentPickerModal. User can change any input's instrument before
 *         adding to the portfolio.
 *
 * Props:
 *   isOpen    {boolean}
 *   onClose   {Function}  () => void
 *   onSelect  {Function}  (signal) => void — receives the (hydrated) signal
 *                         with updated inputs
 */
export default function SignalPickerModal({ isOpen, onClose, onSelect }) {
  const closeRef = useRef(null);

  // Step 1 state — backend-sourced signal list.
  const [category, setCategory] = useState(DEFAULT_CATEGORY);
  const [signals, setSignals] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Step 2 state
  const [selectedSignal, setSelectedSignal] = useState(null);
  const [editedInputs, setEditedInputs] = useState([]);
  const [pickingInputIdx, setPickingInputIdx] = useState(null);

  // Monotonic request token — ignore resolutions from a superseded fetch
  // (rapid category switch, or modal closed mid-flight) so a stale response
  // can't clobber fresher state.
  const reqTokenRef = useRef(0);

  const fetchSignals = useCallback(async (cat) => {
    const token = ++reqTokenRef.current;
    setLoading(true);
    setError(null);
    try {
      const docs = await listSignals(cat);
      if (token !== reqTokenRef.current) return;
      setSignals(Array.isArray(docs) ? docs.map(hydrateFromPersisted) : []);
    } catch (err) {
      if (token !== reqTokenRef.current) return;
      // A failed fetch must NOT collapse into the empty state — surface the
      // error explicitly so the user can retry.
      setError(describePersistenceError(err));
      setSignals([]);
    } finally {
      if (token === reqTokenRef.current) setLoading(false);
    }
  }, []);

  // Fetch on open and whenever the category changes while open.
  useEffect(() => {
    if (!isOpen) return;
    fetchSignals(category);
  }, [isOpen, category, fetchSignals]);

  // Reset all state on close so reopening starts clean (category → default,
  // step 1, no stale list/error). Invalidate any in-flight request.
  useEffect(() => {
    if (!isOpen) {
      reqTokenRef.current += 1;
      setCategory(DEFAULT_CATEGORY);
      setSignals([]);
      setLoading(false);
      setError(null);
      setSelectedSignal(null);
      setEditedInputs([]);
      setPickingInputIdx(null);
    }
  }, [isOpen]);

  // Focus close button on open
  useEffect(() => {
    if (!isOpen) return undefined;
    const t = setTimeout(() => {
      if (closeRef.current) closeRef.current.focus();
    }, 0);
    return () => clearTimeout(t);
  }, [isOpen]);

  // Escape closes (or goes back from step 2)
  useEffect(() => {
    if (!isOpen) return undefined;
    function onKey(e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (pickingInputIdx !== null) {
          setPickingInputIdx(null);
        } else if (selectedSignal) {
          setSelectedSignal(null);
          setEditedInputs([]);
        } else {
          onClose();
        }
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, onClose, selectedSignal, pickingInputIdx]);

  // Step 1 → Step 2 transition
  const handlePickSignal = useCallback((signal) => {
    setSelectedSignal(signal);
    // Deep-copy inputs so edits don't mutate the fetched/hydrated state.
    setEditedInputs(
      (signal.inputs || []).map((inp) => ({
        id: inp.id,
        instrument: inp.instrument ? { ...inp.instrument } : null,
      })),
    );
  }, []);

  // Update an input's instrument from InstrumentPickerModal
  const handleInstrumentSelect = useCallback((instrument) => {
    setEditedInputs((prev) =>
      prev.map((inp, i) => (i === pickingInputIdx ? { ...inp, instrument } : inp)),
    );
    setPickingInputIdx(null);
  }, [pickingInputIdx]);

  // Final "Add to Portfolio"
  const handleConfirm = useCallback(() => {
    if (!selectedSignal) return;
    // Build the signal with updated inputs
    const updated = {
      ...selectedSignal,
      inputs: editedInputs,
    };
    onSelect(updated);
  }, [selectedSignal, editedInputs, onSelect]);

  const handleBack = useCallback(() => {
    setSelectedSignal(null);
    setEditedInputs([]);
  }, []);

  if (!isOpen) return null;

  // All inputs configured? (vacuous truth on empty array intentionally rejected)
  const allConfigured = editedInputs.length > 0 && editedInputs.every((inp) => inp.instrument !== null);

  return (
    <>
      <div
        className={styles.backdrop}
        onMouseDown={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
        data-testid="signal-picker-backdrop"
      >
        <div
          className={styles.card}
          role="dialog"
          aria-modal="true"
          aria-labelledby="signal-picker-title"
          data-testid="signal-picker"
        >
          <div className={styles.header}>
            {selectedSignal && (
              <button
                className={styles.backBtn}
                type="button"
                onClick={handleBack}
                aria-label="Back to signal list"
              >
                &#8592;
              </button>
            )}
            <h3 id="signal-picker-title" className={styles.title}>
              {selectedSignal ? `Configure: ${selectedSignal.name}` : 'Add Signal'}
            </h3>
            <button
              ref={closeRef}
              className={styles.closeBtn}
              type="button"
              onClick={onClose}
              aria-label="Close"
            >
              &#215;
            </button>
          </div>

          {/* ── Step 1: Signal list ── */}
          {!selectedSignal && (
            <>
              {/* Category selector — mirrors the Signals page pattern. */}
              <div className={styles.categoryRow}>
                <label className={styles.categoryLabel} htmlFor="signal-picker-category-select">
                  Category
                </label>
                <select
                  id="signal-picker-category-select"
                  className={styles.categorySelect}
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                  aria-label="Filter signals by category"
                  data-testid="signal-picker-category"
                >
                  {PICKER_CATEGORIES.map((cat) => (
                    <option key={cat} value={cat}>{cat}</option>
                  ))}
                </select>
              </div>

              {loading ? (
                <div className={styles.empty} data-testid="signal-picker-loading">
                  Loading signals&#8230;
                </div>
              ) : error ? (
                <div className={styles.error} data-testid="signal-picker-error">
                  <div className={styles.errorMsg}>
                    <strong>Failed to load signals:</strong> {error}
                  </div>
                  <button
                    className={styles.retryBtn}
                    type="button"
                    onClick={() => fetchSignals(category)}
                  >
                    Retry
                  </button>
                </div>
              ) : signals.length === 0 ? (
                <div className={styles.empty}>
                  No saved signals in this category. Go to the Signals page to create one.
                </div>
              ) : (
                <div className={styles.list}>
                  {signals.map((signal) => {
                    const inputCount = signal.inputs ? signal.inputs.length : 0;
                    const blockCount =
                      (signal.rules?.entries?.length || 0) +
                      (signal.rules?.exits?.length || 0);
                    return (
                      <div key={signal.id} className={styles.signalRow}>
                        <div className={styles.signalInfo}>
                          <div className={styles.signalName}>{signal.name}</div>
                          <div className={styles.signalMeta}>
                            {inputCount} input{inputCount !== 1 ? 's' : ''}
                            {' · '}
                            {blockCount} block{blockCount !== 1 ? 's' : ''}
                          </div>
                        </div>
                        <button
                          className={styles.selectBtn}
                          type="button"
                          onClick={() => handlePickSignal(signal)}
                          aria-label={`Configure signal ${signal.name}`}
                        >
                          Select
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {/* ── Step 2: Configure inputs ── */}
          {selectedSignal && (
            <>
              <div className={styles.inputsSection}>
                <div className={styles.inputsLabel}>
                  Assign an instrument to each input:
                </div>
                <div className={styles.inputsList}>
                  {editedInputs.map((inp, idx) => (
                    <div key={inp.id} className={styles.inputRow}>
                      <span className={styles.inputId}>{inp.id}</span>
                      <span className={styles.inputInstrument}>
                        {inp.instrument
                          ? formatInstrument(inp.instrument)
                          : <span className={styles.unconfigured}>Not configured</span>}
                      </span>
                      <button
                        className={styles.changeBtn}
                        type="button"
                        onClick={() => setPickingInputIdx(idx)}
                      >
                        {inp.instrument ? 'Change' : 'Pick'}
                      </button>
                    </div>
                  ))}
                </div>
                {editedInputs.length === 0 && (
                  <div className={styles.noInputs}>
                    This signal has no inputs.
                  </div>
                )}
              </div>
              <div className={styles.footer}>
                <button
                  className={styles.confirmBtn}
                  type="button"
                  disabled={!allConfigured}
                  onClick={handleConfirm}
                  title={allConfigured ? '' : 'All inputs must be configured'}
                >
                  Add to Portfolio
                </button>
              </div>
            </>
          )}
        </div>
      </div>

      {/* InstrumentPickerModal for rebinding a specific input */}
      <InstrumentPickerModal
        isOpen={pickingInputIdx !== null}
        onClose={() => setPickingInputIdx(null)}
        onSelect={handleInstrumentSelect}
        title={
          pickingInputIdx !== null && editedInputs[pickingInputIdx]
            ? `Pick instrument for input ${editedInputs[pickingInputIdx].id}`
            : 'Select Instrument'
        }
        allowBaskets={true}
      />
    </>
  );
}
