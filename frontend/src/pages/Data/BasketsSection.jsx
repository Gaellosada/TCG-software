import { useMemo, useState } from 'react';
import InstrumentPickerModal from '../../components/InstrumentPickerModal/InstrumentPickerModal';
import { useBasketsList, useInvalidatePersistence } from '../../hooks/persistenceQueries';
import {
  updateBasket,
  archiveBasket,
  describePersistenceError,
  CATEGORIES,
} from '../../api/persistence';
import styles from './CategoryBrowser.module.css';

// Visible (non-archived) basket categories — mirrors the picker modal, which
// lists RESEARCH/DEV/PROD.  ARCHIVE is reachable via the per-row "Archive"
// action but not listed here (archived baskets stay out of the way).
const VISIBLE_CATEGORIES = ['RESEARCH', 'DEV', 'PROD'];

// Map a saved BasketOut into the descriptor DataPage/BasketChart consume.
// Carries the saved ``basket_id`` (so the series endpoint reads the persisted
// legs) AND the inline ``legs`` + ``asset_class`` (so the per-leg breakdown
// overlay works without a second round-trip).
function toSelected(b) {
  return {
    type: 'basket',
    basket: { kind: 'saved', basket_id: b.id },
    basket_id: b.id,
    name: b.name,
    asset_class: b.asset_class,
    legs: b.legs,
  };
}

/**
 * "Baskets" section for the Data page CategoryBrowser.
 *
 * Sourced from app-data (NOT the market-collection categories), so it lives in
 * its own section rather than CATEGORY_CONFIG.  Supports browse + explore +
 * create (reusing the shared InstrumentPickerModal basket composer) + full
 * CRUD (rename / recategorize / archive) via the existing persistence client.
 */
function BasketsSection({ selected, onSelect }) {
  const [expanded, setExpanded] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [renamingId, setRenamingId] = useState(null);
  const [renameDraft, setRenameDraft] = useState('');
  // Surfaces a failed rename/recategorize/archive mutation (reuses the same
  // describePersistenceError helper the Signals page uses) — without it a
  // rejected mutation would silently no-op the UI.
  const [mutationError, setMutationError] = useState(null);
  const invalidate = useInvalidatePersistence();

  // List across the visible categories (one query each), mirroring the modal.
  const research = useBasketsList('RESEARCH');
  const dev = useBasketsList('DEV');
  const prod = useBasketsList('PROD');
  const baskets = useMemo(
    () => [
      ...(Array.isArray(research.data) ? research.data : []),
      ...(Array.isArray(dev.data) ? dev.data : []),
      ...(Array.isArray(prod.data) ? prod.data : []),
    ],
    [research.data, dev.data, prod.data],
  );
  const loading =
    (research.isPending && research.fetchStatus !== 'idle') ||
    (dev.isPending && dev.fetchStatus !== 'idle') ||
    (prod.isPending && prod.fetchStatus !== 'idle');

  function commitRename(b) {
    // Clear any stale error first, before the early-return for an
    // empty/unchanged name — a prior failure must not linger on a no-op.
    setMutationError(null);
    const next = renameDraft.trim();
    setRenamingId(null);
    if (!next || next === b.name) return;
    updateBasket(b.id, {
      name: next,
      category: b.category,
      asset_class: b.asset_class,
      legs: b.legs,
    })
      .then(() => invalidate.baskets(b.id))
      .catch((err) => setMutationError(describePersistenceError(err)));
  }

  function changeCategory(b, nextCat) {
    if (nextCat === b.category) return;
    setMutationError(null);
    updateBasket(b.id, {
      name: b.name,
      category: nextCat,
      asset_class: b.asset_class,
      legs: b.legs,
    })
      .then(() => invalidate.baskets(b.id))
      .catch((err) => setMutationError(describePersistenceError(err)));
  }

  function handleArchive(b) {
    setMutationError(null);
    archiveBasket(b.id)
      .then(() => {
        invalidate.baskets(b.id);
        // If the archived basket was selected, clear the right panel.
        if (selected?.type === 'basket' && selected.basket_id === b.id) onSelect(null);
      })
      .catch((err) => setMutationError(describePersistenceError(err)));
  }

  // Created/saved basket descriptor from the composer — select it and refresh.
  function handlePicked(descriptor) {
    setMutationError(null);
    setPickerOpen(false);
    invalidate.baskets();
    if (!descriptor || descriptor.type !== 'basket') return;
    if (descriptor.kind === 'saved') {
      onSelect({
        type: 'basket',
        basket: { kind: 'saved', basket_id: descriptor.basket_id },
        basket_id: descriptor.basket_id,
        name: descriptor.name,
        asset_class: descriptor.asset_class,
        legs: descriptor.legs,
      });
    } else {
      // Inline (unsaved) basket — explore it directly without persisting.
      onSelect({
        type: 'basket',
        basket: { kind: 'inline', asset_class: descriptor.asset_class, legs: descriptor.legs },
        name: 'Inline basket',
        asset_class: descriptor.asset_class,
        legs: descriptor.legs,
      });
    }
  }

  return (
    <div className={styles.category}>
      <button className={styles.categoryHeader} onClick={() => setExpanded((v) => !v)}>
        <span className={styles.categoryBar} style={{ background: 'var(--cat-assets)' }} />
        <span className={styles.categoryLabel}>Baskets</span>
        <span className={styles.chevron}>{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className={styles.categoryBody}>
          <button
            type="button"
            className={styles.instrument}
            style={{ fontWeight: 600 }}
            onClick={() => setPickerOpen(true)}
          >
            + New basket
          </button>

          {mutationError && (
            <div className={styles.error} role="alert">
              {mutationError}
            </div>
          )}

          {loading && <div className={styles.placeholder}>Loading baskets...</div>}
          {!loading && baskets.length === 0 && (
            <div className={styles.placeholder}>No baskets yet — create one.</div>
          )}

          {baskets.map((b) => {
            const isActive = selected?.type === 'basket' && selected.basket_id === b.id;
            const isRenaming = renamingId === b.id;
            return (
              <div
                key={b.id}
                className={`${styles.instrument} ${isActive ? styles.instrumentActive : ''}`}
                style={{ display: 'flex', alignItems: 'center', gap: '4px' }}
              >
                {isRenaming ? (
                  <input
                    autoFocus
                    value={renameDraft}
                    onChange={(e) => setRenameDraft(e.target.value)}
                    onBlur={() => commitRename(b)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitRename(b);
                      if (e.key === 'Escape') setRenamingId(null);
                    }}
                    style={{ flex: 1, minWidth: 0 }}
                  />
                ) : (
                  <button
                    type="button"
                    onClick={() => onSelect(toSelected(b))}
                    title={b.name}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      textAlign: 'left',
                      background: 'none',
                      border: 'none',
                      color: 'inherit',
                      cursor: 'pointer',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {b.name}
                  </button>
                )}

                <select
                  value={b.category}
                  onChange={(e) => changeCategory(b, e.target.value)}
                  title="Move to category"
                  aria-label={`Category for ${b.name}`}
                  style={{ fontSize: '11px' }}
                >
                  {CATEGORIES.filter((c) => VISIBLE_CATEGORIES.includes(c)).map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>

                <button
                  type="button"
                  title="Rename"
                  aria-label={`Rename ${b.name}`}
                  onClick={() => {
                    setRenamingId(b.id);
                    setRenameDraft(b.name);
                  }}
                  style={{ background: 'none', border: 'none', cursor: 'pointer' }}
                >
                  {'✎'}
                </button>

                <button
                  type="button"
                  title="Archive"
                  aria-label={`Archive ${b.name}`}
                  onClick={() => handleArchive(b)}
                  style={{ background: 'none', border: 'none', cursor: 'pointer' }}
                >
                  {'✕'}
                </button>
              </div>
            );
          })}
        </div>
      )}

      <InstrumentPickerModal
        isOpen={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onSelect={handlePicked}
        title="Create or pick a basket"
        allowBaskets
        // Land the user with the basket composer available; hide non-basket
        // categories so the Data-page basket flow is focused on baskets.
        hiddenCategories={['indexes', 'assets', 'futures', 'options']}
      />
    </div>
  );
}

export default BasketsSection;
