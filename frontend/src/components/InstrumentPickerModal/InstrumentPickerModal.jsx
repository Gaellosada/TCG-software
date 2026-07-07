import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import { createBasket } from '../../api/persistence';
import { useBasketsList, useInvalidatePersistence } from '../../hooks/persistenceQueries';
import OptionStreamForm, { buildDefaultOptionStream, validateOptionStream } from '../OptionStreamForm';
import styles from './InstrumentPickerModal.module.css';

/**
 * Category definitions.
 * Indexes and Assets show instruments directly (no drill-down).
 * Futures and Options keep collection-level navigation (many collections).
 * Baskets opens the inline composer (saved-baskets dropdown + leg builder).
 * The basket category is default-deny: callers must pass `allowBaskets`
 * to surface it.
 */
const CATEGORY_CONFIG = [
  { key: 'indexes', label: 'Indexes', color: 'var(--cat-indexes)', collections: ['INDEX'] },
  { key: 'assets', label: 'Assets', color: 'var(--cat-assets)', collections: ['ETF', 'FOREX', 'FUND'] },
  { key: 'futures', label: 'Futures', color: 'var(--cat-futures)', dynamicFutures: true },
  { key: 'options', label: 'Options', color: 'var(--cat-options)', dynamicOptions: true },
  { key: 'baskets', label: 'Baskets', color: 'var(--cat-baskets, #8b5cf6)', dynamicBaskets: true },
];

const BASKET_ASSET_CLASSES = [
  { key: 'future', label: 'Future' },
  { key: 'option', label: 'Option' },
  { key: 'index', label: 'Index' },
  { key: 'equity', label: 'Equity' },
];

/**
 * Map an asset_class to the candidate collections it spans.
 * Used to scope the per-leg typeahead's instrument list.
 *   - future  → all FUT_* collections
 *   - option  → all OPT_* collections
 *   - index   → ['INDEX']
 *   - equity  → ['ETF']
 */
function collectionsForAssetClass(assetClass, allCollections) {
  if (assetClass === 'future') return allCollections.filter((c) => c.startsWith('FUT_'));
  if (assetClass === 'option') return allCollections.filter((c) => c.startsWith('OPT_'));
  if (assetClass === 'index') return allCollections.filter((c) => c === 'INDEX');
  if (assetClass === 'equity') return allCollections.filter((c) => c === 'ETF');
  return [];
}

/**
 * Shared InstrumentPickerModal — modal dialog for browsing and selecting
 * instruments. Categorized view with expandable groups, drill-down for
 * futures configuration. Used by Portfolio, Indicators, and Signals pages.
 *
 * Emits the v3 InputInstrument discriminated-union value:
 *   - Spot:         { type: 'spot', collection, instrument_id }
 *   - Continuous:   { type: 'continuous', collection, adjustment, cycle,
 *                     rollOffset, strategy: 'front_month' }
 *   - OptionStream: { type: 'option_stream', collection, option_type, cycle,
 *                     maturity, selection, stream, adjustment, roll_offset }
 *   - Basket saved: { type: 'basket', kind: 'saved',  basket_id }
 *   - Basket inline:{ type: 'basket', kind: 'inline', asset_class,
 *                     legs:[{instrument_id, weight}, ...] }
 *
 * Props:
 *   isOpen            {boolean}    whether the modal is visible
 *   onClose           {Function}   () => void — close without selection
 *   onSelect          {Function}   (instrument) => void — called on instrument pick
 *   title             {string?}    modal heading (default: "Select Instrument")
 *   hiddenCategories  {string[]?}  category keys to hide (default: []).
 *                                  e.g. ['options'] to suppress the Options
 *                                  tab on a page that only handles cash/futures.
 *   allowBaskets      {boolean?}   opt-in for the Baskets category.
 *                                  Default false (default-deny). Pages that
 *                                  pick signal inputs (Signals InputsPanel,
 *                                  Portfolio SignalPickerModal) pass true;
 *                                  the instrument-level pickers
 *                                  (AddHoldingModal, Indicators ParamsPanel)
 *                                  leave it default-false so the composer
 *                                  does not surface there.
 */
export default function InstrumentPickerModal({
  isOpen,
  onClose,
  onSelect,
  title,
  hiddenCategories = [],
  allowBaskets = false,
  // Restrict the option-stream picker (the direct Options drill-down) to a
  // subset of streams.  The Portfolio add-holding flow passes ['mid'] so an
  // option leg is the option PRICE only — iv/greeks/volume are SIGNAL-level
  // operands, not a portfolio concern (Issue #2 D1).  ``null`` (default) = no
  // restriction (Data-page chart / signals keep the full stream choice).  Does
  // NOT affect the basket-leg sub-picker (that path is Signals-only).
  optionStreamAllowedStreams = null,
  // SIGNALS-only: surface the option-stream "Hold contract between rolls
  // (fixed-contract P&L)" + nav_times controls in the DIRECT options drill-down
  // (a standalone option signal INPUT).  Default false so Data/Portfolio pickers
  // are unchanged.  NEVER passed to the basket-leg sub-picker (the backend
  // rejects hold_between_rolls on a basket leg — multi-leg held books are
  // Phase-2), so a basket option leg never shows these controls.
  showOptionHoldControls = false,
  // PORTFOLIO option price legs: hold-mode ON only (no toggle; the backend
  // requires it). Forwarded to OptionStreamForm as ``holdRequired``. Never passed
  // to the basket-leg sub-picker (basket held books are unsupported).
  optionHoldRequired = false,
  // Optional reference date (YYYY-MM-DD string or Date) forwarded to
  // OptionStreamForm as ``referenceDate`` — the date at which the implied-
  // leverage readout probes the representative (strike, premium). Falls back to
  // the root's last_trade_date when null.
  optionReferenceDate = null,
}) {
  const [allCollections, setAllCollections] = useState([]);
  const [collectionsLoading, setCollectionsLoading] = useState(false);
  const [collectionsError, setCollectionsError] = useState(null);

  const [instrumentsByCollection, setInstrumentsByCollection] = useState({});
  const [instrumentsLoading, setInstrumentsLoading] = useState(false);

  const [expanded, setExpanded] = useState({});

  // Futures drill-down state
  const [selectedFutCollection, setSelectedFutCollection] = useState(null);
  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [rollOffset, setRollOffset] = useState(2);
  // Roll strategy (Issue #3): 'front_month' (default) or 'end_of_month'.
  const [strategy, setStrategy] = useState('front_month');
  const [availableCycles, setAvailableCycles] = useState([]);

  // Options drill-down state
  const [optionRoots, setOptionRoots] = useState([]);
  const [optionRootsLoading, setOptionRootsLoading] = useState(false);
  const [optionRootsError, setOptionRootsError] = useState(null);
  const [inOptionsDrillDown, setInOptionsDrillDown] = useState(false);
  const [optionStreamValue, setOptionStreamValue] = useState(null);

  // Basket composer state — see Composer state machine below.
  const [inBasketComposer, setInBasketComposer] = useState(false);

  const overlayRef = useRef(null);

  const invalidate = useInvalidatePersistence();

  const visibleCategories = useMemo(
    () => CATEGORY_CONFIG.filter((c) => {
      if (hiddenCategories.includes(c.key)) return false;
      // Default-deny: the basket category needs explicit opt-in. Pages
      // that pick signal inputs pass allowBaskets={true}; instrument-
      // level pickers leave it false so the composer is not surfaced
      // in contexts where a basket descriptor is not a valid selection.
      if (c.key === 'baskets' && !allowBaskets) return false;
      return true;
    }),
    [hiddenCategories, allowBaskets],
  );
  const optionsVisible = useMemo(
    () => visibleCategories.some((c) => c.key === 'options'),
    [visibleCategories],
  );
  const basketsVisible = useMemo(
    () => visibleCategories.some((c) => c.key === 'baskets'),
    [visibleCategories],
  );

  /* ── Saved baskets — TanStack queries (one per category, concatenated) ──
   *
   * Replaces the old open-gated Promise.all([listBaskets×3]) effect. Each
   * category list is its own cached query so invalidate.baskets() (fired after
   * a basket is saved in the composer) refetches all three → the saved-baskets
   * dropdown reflects the new basket without reopening the modal. Enabled only
   * while the modal is open AND baskets are visible (matches the prior gate).
   */
  const basketsEnabled = isOpen && basketsVisible;
  const basketsResearch = useBasketsList('RESEARCH', { enabled: basketsEnabled });
  const basketsDev = useBasketsList('DEV', { enabled: basketsEnabled });
  const basketsProd = useBasketsList('PROD', { enabled: basketsEnabled });
  const basketList = useMemo(() => [
    ...(Array.isArray(basketsResearch.data) ? basketsResearch.data : []),
    ...(Array.isArray(basketsDev.data) ? basketsDev.data : []),
    ...(Array.isArray(basketsProd.data) ? basketsProd.data : []),
  ], [basketsResearch.data, basketsDev.data, basketsProd.data]);
  // Loading only on the cold load (no cached data yet for any category).
  const basketsLoading = basketsEnabled && (
    (basketsResearch.isPending && basketsResearch.fetchStatus !== 'idle')
    || (basketsDev.isPending && basketsDev.fetchStatus !== 'idle')
    || (basketsProd.isPending && basketsProd.fetchStatus !== 'idle')
  );
  const basketsError = (basketsResearch.error || basketsDev.error || basketsProd.error)
    ? (basketsResearch.error || basketsDev.error || basketsProd.error)?.message
      || 'Failed to load baskets'
    : null;

  /* ── Load collections + instruments when modal opens ── */
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;

    setCollectionsLoading(true);
    setCollectionsError(null);

    (async () => {
      try {
        const collections = await listCollections();
        if (cancelled) return;
        setAllCollections(collections);

        setInstrumentsLoading(true);
        const nonFut = CATEGORY_CONFIG
          .filter((c) => !c.dynamicFutures && !c.dynamicOptions && !c.dynamicBaskets)
          .flatMap((c) => c.collections)
          .filter((c) => collections.includes(c));

        const results = await Promise.all(
          nonFut.map(async (coll) => {
            const res = await listInstruments(coll, { skip: 0, limit: 500 });
            return [coll, res.items || []];
          }),
        );

        if (!cancelled) {
          const map = {};
          for (const [coll, items] of results) map[coll] = items;
          setInstrumentsByCollection(map);
          setInstrumentsLoading(false);
        }

        if (!cancelled) setCollectionsLoading(false);
      } catch (err) {
        if (!cancelled) {
          setCollectionsError(err.message);
          setCollectionsLoading(false);
          setInstrumentsLoading(false);
        }
      }
    })();

    return () => { cancelled = true; };
  }, [isOpen]);

  /* ── Load option roots when modal opens ──
   *
   * Loaded whenever the Options tab is visible OR the Baskets tab is
   * visible (the inline composer needs roots to render option-stream
   * legs).  Skipped only when both are gated off.
   */
  useEffect(() => {
    if (!isOpen) return;
    if (!optionsVisible && !basketsVisible) return;
    let cancelled = false;
    setOptionRootsLoading(true);
    setOptionRootsError(null);
    getOptionRoots()
      .then((resp) => {
        if (cancelled) return;
        setOptionRoots(resp.roots || []);
        setOptionRootsLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setOptionRootsError(err?.message || 'Failed to load option roots');
        setOptionRoots([]);
        setOptionRootsLoading(false);
      });
    return () => { cancelled = true; };
  }, [isOpen, optionsVisible, basketsVisible]);

  /* (Saved baskets are loaded by the useBasketsList queries declared above.) */

  /* ── Load available cycles for futures drill-down ── */
  useEffect(() => {
    if (!selectedFutCollection) {
      setAvailableCycles([]);
      return;
    }
    let cancelled = false;
    getAvailableCycles(selectedFutCollection)
      .then((cycles) => { if (!cancelled) setAvailableCycles(cycles); })
      .catch(() => { if (!cancelled) setAvailableCycles([]); });
    return () => { cancelled = true; };
  }, [selectedFutCollection]);

  /* ── ESC to close ── */
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isOpen, onClose]);

  /* ── Reset on close ── */
  useEffect(() => {
    if (!isOpen) {
      setSelectedFutCollection(null);
      setAdjustment('none');
      setCycle('');
      setRollOffset(2);
      setStrategy('front_month');
      setExpanded({});
      setInOptionsDrillDown(false);
      setOptionStreamValue(null);
      setInBasketComposer(false);
    }
  }, [isOpen]);

  const toggleCategory = useCallback((key) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const handleOverlayClick = useCallback(
    (e) => { if (e.target === overlayRef.current) onClose(); },
    [onClose],
  );

  const handleSelectInstrument = useCallback(
    (symbol, collection) => {
      onSelect({ type: 'spot', collection, instrument_id: symbol });
      onClose();
    },
    [onSelect, onClose],
  );

  const handleSelectContinuous = useCallback(
    (collection) => {
      onSelect({
        type: 'continuous',
        collection,
        strategy,
        adjustment,
        cycle: cycle || null,
        rollOffset,
      });
      onClose();
    },
    [adjustment, cycle, rollOffset, strategy, onSelect, onClose],
  );

  const handleBackFromFut = useCallback(() => {
    setSelectedFutCollection(null);
    setAdjustment('none');
    setCycle('');
    setRollOffset(2);
    setStrategy('front_month');
  }, []);

  const handleEnterOptionsDrillDown = useCallback(() => {
    setInOptionsDrillDown(true);
    setOptionStreamValue((prev) => prev || buildDefaultOptionStream({ availableRoots: optionRoots }));
  }, [optionRoots]);

  const handleBackFromOptions = useCallback(() => {
    setInOptionsDrillDown(false);
    setOptionStreamValue(null);
  }, []);

  const handleConfirmOptionStream = useCallback(() => {
    if (!optionStreamValue) return;
    if (validateOptionStream(optionStreamValue, optionRoots) !== null) return;
    onSelect(optionStreamValue);
    onClose();
  }, [optionStreamValue, optionRoots, onSelect, onClose]);

  const handleEnterBasketComposer = useCallback(() => {
    setInBasketComposer(true);
  }, []);

  const handleBackFromBasketComposer = useCallback(() => {
    setInBasketComposer(false);
  }, []);

  /**
   * Composer emits one of two descriptor shapes per the locked contract:
   *   - saved-reference: {type:'basket', kind:'saved',  basket_id}
   *   - inline:          {type:'basket', kind:'inline', asset_class, legs}
   * Emitting closes the picker.
   */
  const handleEmitBasket = useCallback((descriptor) => {
    onSelect(descriptor);
    onClose();
  }, [onSelect, onClose]);

  if (!isOpen) return null;

  const futCollections = allCollections.filter((c) => c.startsWith('FUT_'));
  const inFutDrillDown = selectedFutCollection !== null;
  const futuresVisible = visibleCategories.some((c) => c.key === 'futures');
  const optionStreamValidation = optionStreamValue
    ? validateOptionStream(optionStreamValue, optionRoots)
    : null;
  const confirmDisabled = !optionStreamValue || optionStreamValidation !== null;

  let headerTitle;
  if (inFutDrillDown) headerTitle = selectedFutCollection;
  else if (inOptionsDrillDown) headerTitle = 'Options';
  else if (inBasketComposer) headerTitle = 'Basket Composer';
  else headerTitle = title || 'Select Instrument';

  const inDrillDown = inFutDrillDown || inOptionsDrillDown || inBasketComposer;
  const onBackClick = inFutDrillDown
    ? handleBackFromFut
    : inOptionsDrillDown
      ? handleBackFromOptions
      : handleBackFromBasketComposer;

  return (
    <div
      className={styles.overlay}
      ref={overlayRef}
      onClick={handleOverlayClick}
      role="dialog"
      aria-modal="true"
      aria-label={title || 'Select Instrument'}
    >
      <div className={styles.modal}>
        {/* Header */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            {inDrillDown && (
              <button
                className={styles.backBtn}
                type="button"
                onClick={onBackClick}
              >
                &#8592;
              </button>
            )}
            <h3 className={styles.title}>{headerTitle}</h3>
          </div>
          <button className={styles.closeBtn} type="button" onClick={onClose} aria-label="Close">
            &#215;
          </button>
        </div>

        {/* Body */}
        <div className={styles.body}>
          {collectionsLoading && (
            <div className={styles.state}>Loading...</div>
          )}
          {collectionsError && (
            <div className={styles.error}>{collectionsError}</div>
          )}

          {inOptionsDrillDown ? (
            /* ── Options: pick an OptionStreamRef ── */
            <div className={styles.continuousSection}>
              {optionRootsLoading && <div className={styles.state}>Loading roots...</div>}
              {optionRootsError && <div className={styles.error}>{optionRootsError}</div>}
              {!optionRootsLoading && !optionRootsError && (
                <>
                  <OptionStreamForm
                    value={optionStreamValue}
                    onChange={setOptionStreamValue}
                    availableRoots={optionRoots}
                    showHoldControls={showOptionHoldControls}
                    holdRequired={optionHoldRequired}
                    referenceDate={optionReferenceDate}
                    {...(optionStreamAllowedStreams
                      ? { allowedStreams: optionStreamAllowedStreams }
                      : {})}
                  />
                  <button
                    className={styles.selectContinuousBtn}
                    type="button"
                    onClick={handleConfirmOptionStream}
                    disabled={confirmDisabled}
                    title={optionStreamValidation ? optionStreamValidation.message : undefined}
                    data-testid="option-stream-confirm"
                  >
                    Confirm
                  </button>
                </>
              )}
            </div>
          ) : inFutDrillDown ? (
            /* ── Futures: configure continuous series ──
             *
             * Uses the in-file <ContinuousSpecPicker> sub-component — same
             * single source of truth that <BasketLegRow> uses for future
             * asset_class.  The parent owns adjustment/cycle/rollOffset
             * state (so the iter-0 futures emit shape is unchanged) and
             * passes it down via the picker's value/onChange interface.
             */
            <div className={styles.continuousSection}>
              <p className={styles.continuousText}>
                <strong>{selectedFutCollection}</strong> will be added as a
                continuous rolled series (front month).
              </p>

              <ContinuousSpecPicker
                value={{
                  type: 'continuous',
                  collection: selectedFutCollection,
                  adjustment,
                  cycle: cycle || null,
                  rollOffset,
                  strategy,
                }}
                onChange={(next) => {
                  if (typeof next.adjustment === 'string') setAdjustment(next.adjustment);
                  // Sub-component emits null for "All", parent state is ''.
                  setCycle(next.cycle == null ? '' : next.cycle);
                  if (Number.isFinite(next.rollOffset)) setRollOffset(next.rollOffset);
                  if (typeof next.strategy === 'string') setStrategy(next.strategy);
                }}
                availableCycles={availableCycles}
                assetClass="future"
              />

              <button
                className={styles.selectContinuousBtn}
                type="button"
                onClick={() => handleSelectContinuous(selectedFutCollection)}
              >
                Select Continuous Series
              </button>
            </div>
          ) : inBasketComposer ? (
            /* ── Basket composer: expanding panel ── */
            <BasketComposer
              allCollections={allCollections}
              instrumentsByCollection={instrumentsByCollection}
              basketList={basketList}
              basketsLoading={basketsLoading}
              basketsError={basketsError}
              optionRoots={optionRoots}
              onEmit={handleEmitBasket}
              onBasketSaved={() => invalidate.baskets()}
            />
          ) : (
            /* ── Main view: toggleable categories ── */
            <>
              {visibleCategories.filter((c) => !c.dynamicFutures && !c.dynamicOptions && !c.dynamicBaskets).map((cat) => {
                const instruments = cat.collections.flatMap(
                  (coll) => (instrumentsByCollection[coll] || []).map((inst) => ({ ...inst, collection: coll })),
                );
                if (instruments.length === 0 && !instrumentsLoading) return null;
                const isExpanded = !!expanded[cat.key];
                return (
                  <div key={cat.key} className={styles.group}>
                    <button
                      className={styles.groupToggle}
                      type="button"
                      onClick={() => toggleCategory(cat.key)}
                    >
                      <span className={styles.groupDot} style={{ background: cat.color }} />
                      <span className={styles.groupLabel}>{cat.label}</span>
                      <span className={styles.groupCount}>{instruments.length}</span>
                      <span className={styles.chevron}>{isExpanded ? '▾' : '▸'}</span>
                    </button>
                    {isExpanded && (
                      instrumentsLoading ? (
                        <div className={styles.state}>Loading...</div>
                      ) : (
                        <ul className={styles.instrumentList}>
                          {instruments.map((inst) => (
                            <li
                              key={`${inst.collection}-${inst.symbol}`}
                              className={styles.instrumentItem}
                              role="button"
                              tabIndex={0}
                              onClick={() => handleSelectInstrument(inst.symbol, inst.collection)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') handleSelectInstrument(inst.symbol, inst.collection);
                              }}
                            >
                              <span className={styles.instrumentSymbol}>{inst.symbol}</span>
                            </li>
                          ))}
                        </ul>
                      )
                    )}
                  </div>
                );
              })}

              {/* Futures — collection-level drill-down */}
              {futuresVisible && futCollections.length > 0 && (
                <div className={styles.group}>
                  <button
                    className={styles.groupToggle}
                    type="button"
                    onClick={() => toggleCategory('futures')}
                  >
                    <span className={styles.groupDot} style={{ background: 'var(--cat-futures)' }} />
                    <span className={styles.groupLabel}>Futures</span>
                    <span className={styles.groupCount}>{futCollections.length}</span>
                    <span className={styles.chevron}>{expanded.futures ? '▾' : '▸'}</span>
                  </button>
                  {expanded.futures && (
                    <ul className={styles.collectionList}>
                      {futCollections.map((coll) => (
                        <li
                          key={coll}
                          className={styles.collectionItem}
                          role="button"
                          tabIndex={0}
                          onClick={() => setSelectedFutCollection(coll)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') setSelectedFutCollection(coll);
                          }}
                        >
                          <span>{coll}</span>
                          <span className={styles.chevron}>&#8250;</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {/* Options — drill into stream-form */}
              {optionsVisible && (
                <div className={styles.group}>
                  <button
                    className={styles.groupToggle}
                    type="button"
                    onClick={handleEnterOptionsDrillDown}
                    data-testid="picker-options-toggle"
                  >
                    <span className={styles.groupDot} style={{ background: 'var(--cat-options)' }} />
                    <span className={styles.groupLabel}>Options</span>
                    <span className={styles.groupCount}>
                      {optionRootsLoading ? '...' : optionRoots.length}
                    </span>
                    <span className={styles.chevron}>&#8250;</span>
                  </button>
                </div>
              )}

              {/* Baskets — opens the inline composer */}
              {basketsVisible && (
                <div className={styles.group}>
                  <button
                    className={styles.groupToggle}
                    type="button"
                    onClick={handleEnterBasketComposer}
                    data-testid="picker-baskets-toggle"
                  >
                    <span className={styles.groupDot} style={{ background: 'var(--cat-baskets, #8b5cf6)' }} />
                    <span className={styles.groupLabel}>Baskets</span>
                    <span className={styles.groupCount}>
                      {basketsLoading ? '...' : basketList.length}
                    </span>
                    <span className={styles.chevron}>&#8250;</span>
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Continuous-series spec picker — adjustment / cycle / rollOffset.
 *
 * In-file sub-component (Sign 6: no new file, no nested modal).  The
 * **single source of truth** for the three continuous controls — used
 * both inside the existing futures drill-down (parent owns the value
 * state and passes `availableCycles` from its own loader) AND inside
 * `<BasketLegRow>` per leg for future asset_class (the picker loads its
 * own cycles when no `availableCycles` prop is supplied).
 *
 * Behaviour (Sign 10):
 *   - Identical control labels, identical default values, identical
 *     value shape (`{type:"continuous", collection, adjustment, cycle,
 *     rollOffset, strategy:"front_month"}`) as the iter-0 futures
 *     drill-down JSX.  The extraction is mechanical; the existing
 *     vitests covering the futures flow MUST still pass.
 *
 * Props:
 *   value           — current spec; `value.collection` drives the
 *                     internal cycles loader when `availableCycles` is
 *                     not supplied externally.
 *   onChange        — receives the next full spec object.
 *   availableCycles — when supplied (futures drill-down), the parent
 *                     owns cycle-loading; the picker just renders the
 *                     supplied list.  When undefined (basket leg), the
 *                     picker loads cycles itself keyed off
 *                     `value.collection`.
 *   assetClass      — "future" | "option" — currently informational
 *                     (allows future spec divergence by class).
 */
function ContinuousSpecPicker({ value, onChange, availableCycles, assetClass: _assetClass = 'future' }) {
  const [internalCycles, setInternalCycles] = useState([]);

  // Load cycles when the parent does NOT supply them (basket-leg case).
  // The existing futures drill-down passes its own `availableCycles`
  // from a parent-scoped loader (Sign 10 — behaviour preserved); we
  // skip the internal loader there to avoid duplicate network calls.
  useEffect(() => {
    if (availableCycles !== undefined) return undefined;
    const coll = value && typeof value.collection === 'string' ? value.collection : '';
    if (!coll) {
      setInternalCycles([]);
      return undefined;
    }
    let cancelled = false;
    getAvailableCycles(coll)
      .then((cycles) => { if (!cancelled) setInternalCycles(cycles || []); })
      .catch(() => { if (!cancelled) setInternalCycles([]); });
    return () => { cancelled = true; };
  }, [availableCycles, value && value.collection]);

  const cyclesList = availableCycles !== undefined ? availableCycles : internalCycles;
  const adjustment = (value && value.adjustment) || 'none';
  const cycleRaw = value && value.cycle;
  // The <select> control uses '' to mean "All" (null on the wire).
  const cycleSelect = cycleRaw == null ? '' : cycleRaw;
  const rollOffset = value && Number.isFinite(value.rollOffset) ? value.rollOffset : 0;
  // Roll strategy (Issue #3): 'front_month' (default) or 'end_of_month'.
  const strategy = (value && value.strategy) || 'front_month';

  const emit = useCallback((patch) => {
    const next = {
      type: 'continuous',
      collection: (value && value.collection) || '',
      adjustment,
      cycle: cycleRaw == null ? null : cycleRaw,
      rollOffset,
      strategy,
      ...patch,
    };
    onChange(next);
  }, [value && value.collection, adjustment, cycleRaw, rollOffset, strategy, onChange]);

  return (
    <div className={styles.rollingOptions} data-testid="continuous-spec-picker">
      <label className={styles.optionLabel}>
        Roll strategy
        <select
          className={styles.optionSelect}
          value={strategy}
          onChange={(e) => emit({ strategy: e.target.value })}
          data-testid="continuous-spec-picker-strategy"
        >
          <option value="front_month">Front month (at expiry)</option>
          <option value="end_of_month">End of month</option>
        </select>
      </label>

      <label className={styles.optionLabel}>
        Adjustment
        <select
          className={styles.optionSelect}
          value={adjustment}
          onChange={(e) => emit({ adjustment: e.target.value })}
          data-testid="continuous-spec-picker-adjustment"
        >
          <option value="none">None</option>
          <option value="ratio">Ratio</option>
          <option value="difference">Difference</option>
        </select>
      </label>

      <label className={styles.optionLabel}>
        Cycle
        <select
          className={styles.optionSelect}
          value={cycleSelect}
          onChange={(e) => emit({ cycle: e.target.value === '' ? null : e.target.value })}
          data-testid="continuous-spec-picker-cycle"
        >
          <option value="">All</option>
          {cyclesList.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </label>

      <label className={styles.optionLabel}>
        Roll Offset (days)
        <input
          type="number"
          className={styles.optionSelect}
          style={{ width: '56px' }}
          value={rollOffset}
          min={0}
          max={365}
          onChange={(e) => emit({
            rollOffset: Math.max(0, Math.min(365, parseInt(e.target.value, 10) || 0)),
          })}
          data-testid="continuous-spec-picker-roll-offset"
        />
      </label>
    </div>
  );
}

/**
 * Option-stream spec picker — thin wrapper over the existing
 * <OptionStreamForm> standalone component.  Sub-component of
 * <InstrumentPickerModal> (Sign 6: in-file).
 *
 * Used by <BasketLegRow> for `asset_class="option"`.  The form already
 * builds a BE-compatible OptionStreamRef (`{type:"option_stream",
 * collection, option_type, cycle, maturity, selection, stream,
 * adjustment, roll_offset}`) via
 * `buildDefaultOptionStream`, so the wrapper just initialises the
 * value from `availableRoots` when the leg is empty and forwards
 * subsequent edits through `onChange`.  No new file, no nested modal.
 *
 * Props:
 *   value           — current spec (or null/empty for fresh leg).
 *   onChange        — receives the next spec.
 *   availableRoots  — list of OPT_* roots from getOptionRoots() (loaded
 *                     by the parent modal alongside the Options tab).
 *   assetClass      — currently always "option"; reserved for future
 *                     dispatch parity with ContinuousSpecPicker.
 */
function OptionStreamPicker({ value, onChange, availableRoots, assetClass: _assetClass = 'option' }) {
  // A "complete" value is one with maturity + selection + stream filled
  // in — anything less and we let <OptionStreamForm> render against
  // its own internal default (passing `null` triggers that path inside
  // the form's useMemo guard).  Once the parent adopts the default via
  // the effect below, subsequent renders pass the real value through.
  const valueForForm = (
    value && value.type === 'option_stream'
      && value.maturity && value.selection && value.stream
  ) ? value : null;

  // Adopt a sensible default if the parent has not yet initialised the
  // leg's instrument shape.  We notify the parent so it picks up the
  // baseline immediately (no half-configured state hiding in the form).
  useEffect(() => {
    if (valueForForm !== null) return;
    if (!availableRoots || availableRoots.length === 0) return;
    const next = buildDefaultOptionStream({ availableRoots });
    onChange(next);
    // We intentionally depend only on the availability of roots — once
    // the parent owns a real value we never overwrite it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableRoots && availableRoots.length]);

  return (
    <div data-testid="option-stream-picker" style={{ flex: 1, minWidth: 0 }}>
      <OptionStreamForm
        value={valueForForm}
        onChange={onChange}
        availableRoots={availableRoots || []}
      />
    </div>
  );
}

/**
 * Make a fresh empty leg row for the given basket asset class.
 *
 * Each leg = `{instrument: <Spot|Continuous|OptionStream>, weight}` —
 * the polymorphic shape mirrored on the wire from `BasketLeg` in
 * `tcg/core/api/_models.py`.  The composer never produces a strict-
 * mismatched basket because the per-class branch hard-codes the
 * `instrument.type` it emits (strict-mapping impossibility by
 * construction; pinned by a sanity vitest).
 */
// Monotonic counter for per-leg internal ids.  Used in `makeEmptyLeg`
// so each leg carries a stable React key independent of its array
// index — protects against the class of state-share bug Bug 1 exhibits
// when middle legs are removed and subsequent legs shift index.
// Internal-only: stripped on emit (see `emittableLegs`).
let _legIdCounter = 0;
function nextLegId() {
  _legIdCounter += 1;
  return `leg-${_legIdCounter}`;
}

function makeEmptyLeg(assetClass) {
  if (assetClass === 'future') {
    return {
      __id: nextLegId(),
      instrument: {
        type: 'continuous',
        collection: '',
        adjustment: 'none',
        cycle: null,
        rollOffset: 0,
        strategy: 'front_month',
      },
      weight: 1,
    };
  }
  if (assetClass === 'option') {
    return {
      __id: nextLegId(),
      instrument: {
        // Will be replaced by a full default by <OptionStreamPicker>
        // once `availableRoots` resolves; leaving collection empty
        // here keeps `isInstrumentRefConfigured` false until then.
        type: 'option_stream',
        collection: '',
      },
      weight: 1,
    };
  }
  // equity / index — spot leg.
  return {
    __id: nextLegId(),
    instrument: {
      type: 'spot',
      collection: '',
      instrument_id: '',
    },
    weight: 1,
  };
}

/**
 * True iff a leg's `instrument` sub-object is fully configured for its
 * declared `type` — switched on the discriminator.  Mirrors the
 * server-side per-class refs (`SpotInstrumentRef`,
 * `ContinuousInstrumentRef`, `OptionStreamRef`).  Kept file-local
 * (not imported from `blockShape.js`) so the composer's configuration
 * check has no cross-page dependency; the inline-basket branch in
 * `blockShape.js:isInputConfigured` carries the equivalent dispatch.
 */
function isInstrumentRefConfigured(inst) {
  if (!inst || typeof inst !== 'object') return false;
  if (inst.type === 'spot') {
    return typeof inst.collection === 'string' && inst.collection.length > 0
      && typeof inst.instrument_id === 'string' && inst.instrument_id.length > 0;
  }
  if (inst.type === 'continuous') {
    return typeof inst.collection === 'string' && inst.collection.length > 0;
  }
  if (inst.type === 'option_stream') {
    if (typeof inst.collection !== 'string' || inst.collection.length === 0) return false;
    if (inst.option_type !== 'C' && inst.option_type !== 'P') return false;
    if (!inst.maturity || typeof inst.maturity !== 'object') return false;
    if (!inst.selection || typeof inst.selection !== 'object') return false;
    if (typeof inst.stream !== 'string' || inst.stream.length === 0) return false;
    return true;
  }
  return false;
}

/**
 * Map a basket asset_class to the leg's `instrument.type`.  The
 * composer renderer enforces this mapping structurally (per-class
 * branch hard-codes the emitted type), so a strict-mismatched basket
 * is impossible to produce.
 */
function instrumentTypeForAssetClass(assetClass) {
  if (assetClass === 'future') return 'continuous';
  if (assetClass === 'option') return 'option_stream';
  return 'spot'; // equity, index
}

/**
 * Inline basket composer — sub-component of InstrumentPickerModal.
 *
 * State machine (save/re-save):
 *   pristine    : no save yet                                → emits inline
 *   saved-clean : saved with no edits after save             → emits saved-ref
 *   saved-dirty : saved with at least one edit after save    → emits inline
 *
 * Transitions:
 *   - select saved basket from dropdown  → saved-clean (legs copied in)
 *   - any leg/asset-class mutation       → if savedBasket set, → saved-dirty
 *   - "Save as basket…" confirm success  → saved-clean
 *   - "Unsave"                           → pristine
 *
 * Sign 6: this is a sub-component INSIDE the same file as
 * InstrumentPickerModal; no new modal component, no nested modal.
 */
function BasketComposer({
  allCollections,
  instrumentsByCollection,
  basketList,
  basketsLoading,
  basketsError,
  optionRoots,
  onEmit,
  onBasketSaved,
}) {
  const [assetClass, setAssetClass] = useState('future');
  // Each leg is `{instrument: <discriminated>, weight}`.  See
  // `makeEmptyLeg` for the per-class default shape.  This polymorphic
  // shape mirrors `BasketLeg` on the BE wire.
  const [legs, setLegs] = useState(() => [makeEmptyLeg('future')]);
  const [selectedSavedId, setSelectedSavedId] = useState('');
  // savedBasket: {id, name} | null. Non-null means current legs reflect a
  // saved basket (possibly with edits, see dirtySinceSave).
  const [savedBasket, setSavedBasket] = useState(null);
  const [dirtySinceSave, setDirtySinceSave] = useState(false);
  // Inline save-as input state.
  const [saveInputOpen, setSaveInputOpen] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [saveError, setSaveError] = useState(null);
  const [saving, setSaving] = useState(false);
  // Per-leg instrument cache (loaded on-demand for asset classes whose
  // collections weren't pre-fetched: futures and options).
  const [extraInstrumentsByCollection, setExtraInstrumentsByCollection] = useState({});

  // Pending asset-class change confirmation (fired when a user changes
  // asset_class while legs contain configured rows).
  const [pendingAssetClass, setPendingAssetClass] = useState(null);

  // Combined instruments-by-collection used for typeahead candidates.
  const allInstrumentsByCollection = useMemo(() => ({
    ...instrumentsByCollection,
    ...extraInstrumentsByCollection,
  }), [instrumentsByCollection, extraInstrumentsByCollection]);

  // Candidate collections for the currently picked asset class.
  const candidateCollections = useMemo(
    () => collectionsForAssetClass(assetClass, allCollections),
    [assetClass, allCollections],
  );

  // Load any missing instrument lists for the current asset class.
  // (Only used by the spot typeahead — futures/options legs pick a
  // collection rather than a per-contract symbol.)
  useEffect(() => {
    let cancelled = false;
    const missing = candidateCollections.filter(
      (c) => !(c in instrumentsByCollection) && !(c in extraInstrumentsByCollection),
    );
    if (missing.length === 0) return undefined;
    (async () => {
      const fetched = {};
      for (const coll of missing) {
        try {
          const res = await listInstruments(coll, { skip: 0, limit: 500 });
          fetched[coll] = res.items || [];
        } catch {
          fetched[coll] = [];
        }
      }
      if (!cancelled) {
        setExtraInstrumentsByCollection((prev) => ({ ...prev, ...fetched }));
      }
    })();
    return () => { cancelled = true; };
  }, [candidateCollections, instrumentsByCollection, extraInstrumentsByCollection]);

  // Combined candidate-instrument list for the spot typeahead, scoped
  // to the current asset class.  Each entry carries `collection`.
  const candidateInstruments = useMemo(() => {
    const out = [];
    for (const coll of candidateCollections) {
      const items = allInstrumentsByCollection[coll] || [];
      for (const inst of items) {
        out.push({ symbol: inst.symbol, collection: coll });
      }
    }
    return out;
  }, [candidateCollections, allInstrumentsByCollection]);

  // True when at least one leg's instrument is fully configured AND
  // weight is a finite non-zero number.  CTAs stay disabled otherwise.
  const hasConfiguredLeg = useMemo(
    () => legs.some(
      (l) => isInstrumentRefConfigured(l.instrument)
        && Number.isFinite(l.weight) && l.weight !== 0,
    ),
    [legs],
  );

  // Subset of legs that are emit-ready (drop empty rows so a half-
  // filled composer can still emit only the populated rows).  The
  // emitted leg shape matches the BE `BasketLeg` polymorphic wire
  // contract: `{instrument: <sub-object>, weight}`.
  const emittableLegs = useMemo(
    () => legs
      .filter(
        (l) => isInstrumentRefConfigured(l.instrument)
          && Number.isFinite(l.weight) && l.weight !== 0,
      )
      .map((l) => ({ instrument: l.instrument, weight: l.weight })),
    [legs],
  );

  /** Mark composer dirty if a saved basket is loaded. */
  const markDirtyIfSaved = useCallback(() => {
    if (savedBasket) setDirtySinceSave(true);
  }, [savedBasket]);

  /** Replace a leg's `instrument` sub-object wholesale. */
  const setLegInstrument = useCallback((idx, instrument) => {
    setLegs((prev) => {
      const next = prev.slice();
      next[idx] = { ...next[idx], instrument };
      return next;
    });
    markDirtyIfSaved();
  }, [markDirtyIfSaved]);

  const setLegWeight = useCallback((idx, weight) => {
    setLegs((prev) => {
      const next = prev.slice();
      next[idx] = { ...next[idx], weight };
      return next;
    });
    markDirtyIfSaved();
  }, [markDirtyIfSaved]);

  const removeLeg = useCallback((idx) => {
    setLegs((prev) => {
      const next = prev.slice();
      next.splice(idx, 1);
      // Never leave zero leg rows in the composer UI — fall back to a
      // single empty row of the current asset class so the user always
      // sees the editing affordance.
      return next.length === 0 ? [makeEmptyLeg(assetClass)] : next;
    });
    markDirtyIfSaved();
  }, [markDirtyIfSaved, assetClass]);

  const addLeg = useCallback(() => {
    setLegs((prev) => [...prev, makeEmptyLeg(assetClass)]);
    markDirtyIfSaved();
  }, [markDirtyIfSaved, assetClass]);

  /** Asset-class change: confirm if any leg is populated; clear legs. */
  const requestAssetClassChange = useCallback((next) => {
    if (next === assetClass) return;
    // A leg is "non-empty" if its instrument has at least the
    // collection slot populated OR the spot instrument_id (the user
    // started filling it in).  We treat the fresh-from-`makeEmptyLeg`
    // shape as empty even though it has `type` set.
    const hasNonEmpty = legs.some((l) => {
      const inst = l.instrument || {};
      if (inst.type === 'spot') return !!(inst.collection || inst.instrument_id);
      if (inst.type === 'continuous') return !!inst.collection;
      if (inst.type === 'option_stream') return !!inst.collection;
      return false;
    });
    if (hasNonEmpty) {
      setPendingAssetClass(next);
    } else {
      setAssetClass(next);
      // Replace all empty legs with empty legs of the new asset class
      // so the leg-state shape stays consistent with the renderer
      // dispatch (strict-mapping impossibility by construction).
      setLegs([makeEmptyLeg(next)]);
      markDirtyIfSaved();
    }
  }, [assetClass, legs, markDirtyIfSaved]);

  const confirmAssetClassChange = useCallback(() => {
    if (!pendingAssetClass) return;
    setAssetClass(pendingAssetClass);
    setLegs([makeEmptyLeg(pendingAssetClass)]);
    setPendingAssetClass(null);
    markDirtyIfSaved();
  }, [pendingAssetClass, markDirtyIfSaved]);

  const cancelAssetClassChange = useCallback(() => {
    setPendingAssetClass(null);
  }, []);

  /** Load a saved basket into the composer as an inline copy. */
  const handleSelectSaved = useCallback((basketId) => {
    setSelectedSavedId(basketId);
    if (!basketId) {
      // User cleared the dropdown — keep current legs but drop saved-ref.
      setSavedBasket(null);
      setDirtySinceSave(false);
      return;
    }
    const found = basketList.find((b) => b.id === basketId);
    if (!found) return;
    // BE persists `BasketDoc.asset_class` alongside the polymorphic
    // legs — trust the envelope.  Fall back to the current selection
    // if the envelope is missing (defensive; production rows always
    // carry it).
    const nextAssetClass = (
      found.asset_class === 'future' || found.asset_class === 'option'
        || found.asset_class === 'index' || found.asset_class === 'equity'
    ) ? found.asset_class : assetClass;
    setAssetClass(nextAssetClass);
    const expectedType = instrumentTypeForAssetClass(nextAssetClass);
    setLegs(
      (found.legs || []).map((l) => {
        // `l.instrument` is opaque on the wire — adopt it verbatim
        // when its type matches the envelope.  Strict-mismatched
        // persisted legs would have been rejected by the BE CRUD
        // validator on write; if one slips in we fall back to an
        // empty leg of the right type.
        const inst = (l && l.instrument && l.instrument.type === expectedType)
          ? l.instrument
          : makeEmptyLeg(nextAssetClass).instrument;
        return {
          __id: nextLegId(),
          instrument: inst,
          weight: typeof l.weight === 'number' ? l.weight : 1,
        };
      }),
    );
    setSavedBasket({ id: found.id, name: found.name || found.id });
    setDirtySinceSave(false);
    // Drop any in-progress save-input.
    setSaveInputOpen(false);
    setSaveName('');
    setSaveError(null);
  }, [basketList, assetClass]);

  /** Emit the current composition (saved-ref OR inline, per state). */
  const handleUseComposition = useCallback(() => {
    if (!hasConfiguredLeg) return;
    if (savedBasket && !dirtySinceSave) {
      onEmit({ type: 'basket', kind: 'saved', basket_id: savedBasket.id });
      return;
    }
    onEmit({
      type: 'basket',
      kind: 'inline',
      asset_class: assetClass,
      legs: emittableLegs,
    });
  }, [hasConfiguredLeg, savedBasket, dirtySinceSave, assetClass, emittableLegs, onEmit]);

  /** Open the inline name input for "Save as basket…". */
  const openSaveInput = useCallback(() => {
    if (!hasConfiguredLeg) return;
    setSaveInputOpen(true);
    setSaveName(savedBasket?.name || '');
    setSaveError(null);
  }, [hasConfiguredLeg, savedBasket]);

  const cancelSaveInput = useCallback(() => {
    setSaveInputOpen(false);
    setSaveName('');
    setSaveError(null);
  }, []);

  /** Confirm save: POST createBasket, then transition to saved-clean. */
  const confirmSave = useCallback(async () => {
    if (!hasConfiguredLeg) return;
    const trimmed = saveName.trim();
    if (!trimmed) {
      setSaveError('Name is required');
      return;
    }
    // Generate a stable id from the name + timestamp; the BE may
    // rewrite the id (we adopt whatever it confirms).
    const slug = trimmed
      .replace(/[^a-zA-Z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .toUpperCase()
      .slice(0, 32) || 'BASKET';
    const id = `BSK_${slug}_${Date.now()}`;
    setSaving(true);
    setSaveError(null);
    try {
      const created = await createBasket({
        id,
        name: trimmed,
        category: 'RESEARCH',
        asset_class: assetClass,
        // Polymorphic leg shape on the wire — `instrument` is the
        // discriminated sub-object, `weight` is signed/non-zero.  The
        // BE `BasketIn` Pydantic model validates the per-class strict
        // mapping (asset_class → instrument.type).
        legs: legs
          .filter(
            (l) => isInstrumentRefConfigured(l.instrument)
              && Number.isFinite(l.weight) && l.weight !== 0,
          )
          .map((l) => ({ instrument: l.instrument, weight: l.weight })),
      });
      // Use whatever id the BE confirms (it may rewrite ours).
      const finalId = (created && created.id) || id;
      const finalName = (created && created.name) || trimmed;
      setSavedBasket({ id: finalId, name: finalName });
      setDirtySinceSave(false);
      setSaveInputOpen(false);
      setSaveName('');
      // Invalidate the saved-baskets queries so the dropdown reflects the
      // newly-saved basket (new RESEARCH-category doc) without reopening.
      if (onBasketSaved) onBasketSaved();
    } catch (err) {
      setSaveError(err?.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  }, [hasConfiguredLeg, saveName, legs, assetClass, onBasketSaved]);

  const handleUnsave = useCallback(() => {
    setSavedBasket(null);
    setDirtySinceSave(false);
    setSelectedSavedId('');
  }, []);

  const ctaDisabled = !hasConfiguredLeg;
  const usingSavedRef = !!(savedBasket && !dirtySinceSave);

  return (
    <div data-testid="basket-composer" className={styles.continuousSection} style={{ alignItems: 'stretch' }}>
      {/* Saved basket dropdown + asset-class selector */}
      <div className={styles.rollingOptions}>
        <label className={styles.optionLabel}>
          Saved
          <select
            className={styles.optionSelect}
            value={selectedSavedId}
            onChange={(e) => handleSelectSaved(e.target.value)}
            disabled={basketsLoading}
            data-testid="basket-saved-select"
          >
            <option value="">— select —</option>
            {basketList.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name || b.id}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.optionLabel}>
          Asset
          <select
            className={styles.optionSelect}
            value={assetClass}
            onChange={(e) => requestAssetClassChange(e.target.value)}
            data-testid="basket-asset-class-select"
          >
            {BASKET_ASSET_CLASSES.map((ac) => (
              <option key={ac.key} value={ac.key}>{ac.label}</option>
            ))}
          </select>
        </label>
      </div>

      {basketsError && (
        <div className={styles.error} data-testid="basket-list-error">
          {basketsError}
        </div>
      )}

      {/* Asset-class change confirmation banner */}
      {pendingAssetClass && (
        <div
          data-testid="basket-asset-class-confirm"
          style={{
            background: 'var(--bg-hover)',
            border: '1px solid var(--border-primary)',
            padding: '8px 12px',
            borderRadius: 'var(--radius-sm)',
            fontSize: '0.85rem',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            justifyContent: 'space-between',
          }}
        >
          <span>
            Switching asset class will clear all legs. Continue?
          </span>
          <span style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              className={styles.selectContinuousBtn}
              style={{ padding: '4px 10px', fontSize: '0.8rem' }}
              onClick={confirmAssetClassChange}
              data-testid="basket-asset-class-confirm-yes"
            >
              Confirm
            </button>
            <button
              type="button"
              className={styles.selectContinuousBtn}
              style={{ padding: '4px 10px', fontSize: '0.8rem', background: 'var(--bg-primary)', color: 'var(--text-primary)', border: '1px solid var(--border-primary)' }}
              onClick={cancelAssetClassChange}
              data-testid="basket-asset-class-confirm-cancel"
            >
              Cancel
            </button>
          </span>
        </div>
      )}

      {/* Saved banner */}
      {savedBasket && (
        <div
          data-testid="basket-saved-banner"
          style={{
            background: 'var(--bg-hover)',
            border: '1px solid var(--border-primary)',
            padding: '8px 12px',
            borderRadius: 'var(--radius-sm)',
            fontSize: '0.85rem',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            justifyContent: 'space-between',
          }}
        >
          <span>
            {dirtySinceSave
              ? <>Modified — re-save to keep changes (current selection emits inline).</>
              : <>&#10003; Saved as &quot;{savedBasket.name}&quot;</>
            }
          </span>
          <button
            type="button"
            className={styles.closeBtn}
            onClick={handleUnsave}
            data-testid="basket-unsave-btn"
            title="Drop the saved reference; current legs remain in the composer."
            style={{ fontSize: '0.8rem' }}
          >
            Unsave
          </button>
        </div>
      )}

      {/* Legs */}
      <div data-testid="basket-legs" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {legs.map((leg, idx) => (
          <BasketLegRow
            key={leg.__id || idx}
            leg={leg}
            assetClass={assetClass}
            candidateInstruments={candidateInstruments}
            candidateCollections={candidateCollections}
            optionRoots={optionRoots}
            onChangeInstrument={(instrument) => setLegInstrument(idx, instrument)}
            onChangeWeight={(w) => setLegWeight(idx, w)}
            onRemove={() => removeLeg(idx)}
            testId={`basket-leg-${idx}`}
          />
        ))}
        <button
          type="button"
          onClick={addLeg}
          data-testid="basket-add-leg"
          style={{
            background: 'transparent',
            border: '1px dashed var(--border-primary)',
            color: 'var(--text-secondary)',
            padding: '6px 12px',
            borderRadius: 'var(--radius-sm)',
            cursor: 'pointer',
            fontSize: '0.85rem',
            alignSelf: 'flex-start',
          }}
        >
          + Add leg
        </button>
      </div>

      {/* Save-as inline input (NOT a modal — Sign 6) */}
      {saveInputOpen && (
        <div
          data-testid="basket-save-input"
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
            padding: '8px 12px',
            background: 'var(--bg-hover)',
            border: '1px solid var(--border-primary)',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          <label className={styles.optionLabel} style={{ width: '100%' }}>
            Basket name
            <input
              type="text"
              className={styles.optionSelect}
              style={{ flex: 1 }}
              value={saveName}
              onChange={(e) => setSaveName(e.target.value)}
              disabled={saving}
              data-testid="basket-save-name-input"
              autoFocus
            />
          </label>
          {saveError && (
            <span className={styles.error} data-testid="basket-save-error" style={{ padding: 0 }}>
              {saveError}
            </span>
          )}
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              className={styles.selectContinuousBtn}
              style={{ padding: '4px 12px', fontSize: '0.8rem' }}
              onClick={confirmSave}
              disabled={saving || !saveName.trim()}
              data-testid="basket-save-confirm"
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
            <button
              type="button"
              className={styles.selectContinuousBtn}
              style={{ padding: '4px 12px', fontSize: '0.8rem', background: 'var(--bg-primary)', color: 'var(--text-primary)', border: '1px solid var(--border-primary)' }}
              onClick={cancelSaveInput}
              disabled={saving}
              data-testid="basket-save-cancel"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* CTAs */}
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', borderTop: '1px solid var(--border-primary)', paddingTop: 12 }}>
        <button
          type="button"
          className={styles.selectContinuousBtn}
          style={{ background: 'var(--bg-primary)', color: 'var(--text-primary)', border: '1px solid var(--border-primary)' }}
          onClick={openSaveInput}
          disabled={ctaDisabled || saveInputOpen || (usingSavedRef && !dirtySinceSave)}
          data-testid="basket-save-btn"
        >
          {usingSavedRef ? 'Saved ✓' : (savedBasket && dirtySinceSave ? 'Re-save…' : 'Save as basket…')}
        </button>
        <button
          type="button"
          className={styles.selectContinuousBtn}
          onClick={handleUseComposition}
          disabled={ctaDisabled}
          data-testid="basket-use-btn"
        >
          {usingSavedRef ? 'Use saved basket' : 'Use without saving'}
        </button>
      </div>
    </div>
  );
}

/**
 * Per-leg row — dispatches the per-instrument renderer by
 * `assetClass`.  Sub-component of BasketComposer (Sign 6 — same file,
 * no nested modal).
 *
 *   - equity / index → spot typeahead (iter-1/2 UX) → emits
 *                      `{type:"spot", collection, instrument_id}`.
 *   - future         → collection picker + <ContinuousSpecPicker> →
 *                      emits `{type:"continuous", collection,
 *                      adjustment, cycle, rollOffset,
 *                      strategy:"front_month"}`.
 *   - option         → <OptionStreamPicker> (wraps OptionStreamForm
 *                      which has its own root selector) → emits the
 *                      full `OptionStreamRef` shape.
 *
 * Weight + remove control are common to all three.
 */
function BasketLegRow({
  leg,
  assetClass,
  candidateInstruments,
  candidateCollections,
  optionRoots,
  onChangeInstrument,
  onChangeWeight,
  onRemove,
  testId,
}) {
  const weightValid = Number.isFinite(leg.weight) && leg.weight !== 0;
  const inst = leg.instrument || {};

  // Per-renderer body.  All three end with the weight + remove
  // controls so the layout stays consistent across asset classes.
  let body;
  if (inst.type === 'spot') {
    body = (
      <SpotLegPicker
        leg={leg}
        candidateInstruments={candidateInstruments}
        onChangeInstrument={onChangeInstrument}
        testId={testId}
      />
    );
  } else if (inst.type === 'continuous') {
    body = (
      <ContinuousLegPicker
        leg={leg}
        candidateCollections={candidateCollections}
        onChangeInstrument={onChangeInstrument}
        testId={testId}
      />
    );
  } else if (inst.type === 'option_stream') {
    body = (
      <OptionLegPicker
        leg={leg}
        optionRoots={optionRoots}
        onChangeInstrument={onChangeInstrument}
        testId={testId}
      />
    );
  } else {
    // Fallback — should be unreachable because every makeEmptyLeg
    // branch sets `instrument.type` and the renderer dispatch above
    // covers all three discriminants.
    body = <div data-testid={`${testId}-unknown-type`}>Unsupported asset class</div>;
  }

  return (
    <div
      data-testid={testId}
      data-asset-class={assetClass}
      data-instrument-type={inst.type || ''}
      style={{
        display: 'flex',
        alignItems: inst.type === 'option_stream' ? 'flex-start' : 'center',
        gap: 8,
        padding: '6px 8px',
        border: '1px solid var(--border-primary)',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--bg-primary)',
        position: 'relative',
      }}
    >
      {body}
      <input
        type="number"
        step="any"
        className={styles.optionSelect}
        style={{ width: 80 }}
        value={Number.isFinite(leg.weight) ? leg.weight : ''}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === '' || raw === '-') {
            // Allow intermediate states while typing; mark as NaN so
            // hasConfiguredLeg rejects until a real number is entered.
            onChangeWeight(NaN);
            return;
          }
          const parsed = parseFloat(raw);
          onChangeWeight(Number.isFinite(parsed) ? parsed : NaN);
        }}
        placeholder="±1.0"
        data-testid={`${testId}-weight-input`}
        aria-invalid={!weightValid}
      />
      <button
        type="button"
        className={styles.closeBtn}
        onClick={onRemove}
        aria-label="Remove leg"
        data-testid={`${testId}-remove`}
        style={{ fontSize: '1rem' }}
      >
        &#215;
      </button>
    </div>
  );
}

/**
 * Spot leg picker — instrument typeahead.  Used for equity / index.
 * Sub-component of <BasketLegRow> (in-file; Sign 6).
 */
function SpotLegPicker({ leg, candidateInstruments, onChangeInstrument, testId }) {
  const inst = leg.instrument || {};
  const [query, setQuery] = useState(inst.instrument_id || '');
  const [showSuggestions, setShowSuggestions] = useState(false);

  // Keep typeahead text in sync when the leg's instrument changes
  // via saved-basket load (external update).
  useEffect(() => {
    setQuery(inst.instrument_id || '');
  }, [inst.instrument_id]);

  const filtered = useMemo(() => {
    const q = (query || '').trim().toUpperCase();
    if (!q) return candidateInstruments.slice(0, 20);
    return candidateInstruments
      .filter((c) => c.symbol.toUpperCase().includes(q))
      .slice(0, 20);
  }, [query, candidateInstruments]);

  return (
    <div style={{ flex: 1, position: 'relative' }}>
      <input
        type="text"
        className={styles.optionSelect}
        style={{ width: '100%' }}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setShowSuggestions(true);
          // Reset committed instrument until the user picks one — avoids
          // emitting a typed-but-unconfirmed string as instrument_id.
          if (inst.instrument_id) {
            onChangeInstrument({ type: 'spot', collection: '', instrument_id: '' });
          }
        }}
        onFocus={() => setShowSuggestions(true)}
        onBlur={() => {
          // Delay so onClick on a suggestion has time to fire.
          setTimeout(() => setShowSuggestions(false), 120);
        }}
        placeholder="Search instrument..."
        data-testid={`${testId}-instrument-input`}
      />
      {showSuggestions && filtered.length > 0 && (
        <ul
          data-testid={`${testId}-suggestions`}
          style={{
            listStyle: 'none',
            margin: 0,
            padding: 0,
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            maxHeight: 180,
            overflowY: 'auto',
            background: 'var(--bg-surface)',
            border: '1px solid var(--border-primary)',
            borderRadius: 'var(--radius-sm)',
            zIndex: 1100,
          }}
        >
          {filtered.map((c) => (
            <li
              key={`${c.collection}-${c.symbol}`}
              style={{
                padding: '4px 8px',
                cursor: 'pointer',
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: '0.8rem',
              }}
              role="button"
              tabIndex={0}
              onMouseDown={(e) => {
                // Use mousedown so it fires before the input's onBlur.
                e.preventDefault();
                setQuery(c.symbol);
                setShowSuggestions(false);
                onChangeInstrument({
                  type: 'spot',
                  collection: c.collection,
                  instrument_id: c.symbol,
                });
              }}
              data-testid={`${testId}-suggestion-${c.symbol}`}
            >
              <span>{c.symbol}</span>
              <span style={{ marginLeft: 8, opacity: 0.6 }}>({c.collection})</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Continuous (future) leg picker — collection select +
 * <ContinuousSpecPicker>.  Sub-component of <BasketLegRow>.
 *
 * The future-asset-class leg references a continuous-rolled series of
 * a futures collection (e.g., FUT_ES), not a specific contract.  The
 * collection dropdown is scoped to FUT_* collections; the
 * <ContinuousSpecPicker> sub-component (single source of truth shared
 * with the existing futures drill-down) handles adjustment / cycle /
 * rollOffset.
 */
function ContinuousLegPicker({ leg, candidateCollections, onChangeInstrument, testId }) {
  const inst = leg.instrument || {};
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
      <select
        className={styles.optionSelect}
        style={{ width: '100%' }}
        value={inst.collection || ''}
        onChange={(e) => onChangeInstrument({
          ...inst,
          type: 'continuous',
          collection: e.target.value,
          // Preserve the leg's chosen roll strategy (Issue #3) — do NOT hardcode
          // it, or picking a collection would silently revert end_of_month.
          strategy: inst.strategy || 'front_month',
        })}
        data-testid={`${testId}-collection-select`}
      >
        <option value="">— pick a collection —</option>
        {candidateCollections.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>
      <ContinuousSpecPicker
        value={{
          type: 'continuous',
          collection: inst.collection || '',
          adjustment: inst.adjustment || 'none',
          cycle: inst.cycle == null ? null : inst.cycle,
          rollOffset: Number.isFinite(inst.rollOffset) ? inst.rollOffset : 0,
          // Read strategy from the leg (Issue #3): hardcoding front_month here
          // would (a) display the wrong value and (b) make ContinuousSpecPicker's
          // value-spreading emit silently revert end_of_month on any other edit.
          strategy: inst.strategy || 'front_month',
        }}
        onChange={(next) => onChangeInstrument(next)}
        availableCycles={undefined /* sub-component loads its own keyed off value.collection */}
        assetClass="future"
      />
    </div>
  );
}

/**
 * Option leg picker — wraps <OptionStreamPicker> which in turn
 * wraps the existing <OptionStreamForm>.  Sub-component of
 * <BasketLegRow>.
 */
function OptionLegPicker({ leg, optionRoots, onChangeInstrument, testId }) {
  const inst = leg.instrument || {};
  return (
    <div style={{ flex: 1, display: 'flex' }} data-testid={`${testId}-option-leg`}>
      <OptionStreamPicker
        value={inst.type === 'option_stream' ? inst : null}
        onChange={(next) => onChangeInstrument(next)}
        availableRoots={optionRoots}
        assetClass="option"
      />
    </div>
  );
}
