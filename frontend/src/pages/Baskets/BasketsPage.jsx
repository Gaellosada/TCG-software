// BasketsPage — CRUD UI for persisted Basket documents.
//
// A Basket is a named, asset-class-homogeneous, signed-weighted group of
// instrument legs. The page mirrors the architecture of PortfolioPage:
//
//   - left side: category filter + list of saved baskets + create form
//   - right side: editor for the currently selected basket (name + legs)
//   - autosave: debounced PUT on edit via useBackendAutosave, with abort
//     + in-flight coalescing
//   - reselect guard: clicking the SAME loaded basket while local edits
//     are pending does NOT clobber state — the autosave hook flushes those
//     edits to the backend instead.
//
// The wire contract for a basket is:
//   { id, type:"basket", name, category, created_at, updated_at,
//     legs:[{instrument_id, collection, weight}] }
//
// Negative weight = short leg. The backend rejects zero-weight legs.

import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import SaveStatus from '../../components/SaveStatus/SaveStatus';
import ConfirmDialog from '../../components/ConfirmDialog';
import Card from '../../components/Card';
import useBackendAutosave from '../../hooks/useBackendAutosave';
import {
  CATEGORIES,
  listBaskets,
  createBasket,
  getBasket,
  updateBasket,
  archiveBasket,
  describePersistenceError,
} from '../../api/persistence';
import styles from './BasketsPage.module.css';

/**
 * Normalise legs from a BasketOut into the editor's local shape. The
 * editor stores the same field names as the wire shape; we just guard
 * against missing arrays.
 */
function normaliseLegs(legs) {
  if (!Array.isArray(legs)) return [];
  const out = [];
  for (const l of legs) {
    if (!l || typeof l !== 'object') continue;
    out.push({
      instrument_id: typeof l.instrument_id === 'string' ? l.instrument_id : '',
      collection: typeof l.collection === 'string' ? l.collection : '',
      weight: Number(l.weight),
    });
  }
  return out;
}

function BasketsPage() {
  // --- List / category state ---
  const [category, setCategory] = useState('RESEARCH');
  const [baskets, setBaskets] = useState([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState(null);

  // --- Editor state (current selection) ---
  const [selectedId, setSelectedId] = useState(null);
  const [editorName, setEditorName] = useState('');
  const [editorCategory, setEditorCategory] = useState('RESEARCH');
  const [editorLegs, setEditorLegs] = useState([]);
  const [editorLoading, setEditorLoading] = useState(false);

  // --- One-shot status (create / archive / category move) ---
  const [oneshotStatus, setOneshotStatus] = useState('idle');
  const [oneshotError, setOneshotError] = useState(null);

  // --- Cloud autosave error (most-recent debounced PUT failure) ---
  const [cloudError, setCloudError] = useState(null);

  // --- Create form ---
  const [newId, setNewId] = useState('');
  const [newName, setNewName] = useState('');

  // --- Add-leg form ---
  const [newLegId, setNewLegId] = useState('');
  const [newLegCollection, setNewLegCollection] = useState('ETF');
  const [newLegWeight, setNewLegWeight] = useState('');

  // --- Archive confirmation target ---
  const [archiveTarget, setArchiveTarget] = useState(null);

  // -------------------------------------------------------------------------
  // List fetching
  // -------------------------------------------------------------------------

  const fetchBaskets = useCallback(async (cat) => {
    setListLoading(true);
    setListError(null);
    try {
      const items = await listBaskets(cat);
      setBaskets(Array.isArray(items) ? items : []);
    } catch (err) {
      setListError(describePersistenceError(err));
      setBaskets([]);
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchBaskets(category);
  }, [category, fetchBaskets]);

  // -------------------------------------------------------------------------
  // Selection / hydration
  // -------------------------------------------------------------------------

  // Mirror of cloudDirty so handleSelect (declared before the autosave
  // memo) can read the current value without a closure dependency.
  const cloudDirtyRef = useRef(false);

  const handleSelect = useCallback(async (id) => {
    // Reselect guard — if clicking the row we already have loaded AND
    // there are pending unsaved edits, do NOT clobber state with a
    // stale backend snapshot. The autosave hook will push the edits.
    if (id === selectedId && cloudDirtyRef.current) return;
    setEditorLoading(true);
    try {
      const basket = await getBasket(id);
      setSelectedId(basket.id);
      setEditorName(basket.name || '');
      setEditorCategory(basket.category || 'RESEARCH');
      setEditorLegs(normaliseLegs(basket.legs));
      setCloudError(null);
    } catch (err) {
      setListError(describePersistenceError(err));
    } finally {
      setEditorLoading(false);
    }
  }, [selectedId]);

  // -------------------------------------------------------------------------
  // Autosave wiring
  // -------------------------------------------------------------------------

  // Serialise the editor state into a stable JSON string. A reference
  // change on this string triggers the debounce timer.
  const cloudPayload = useMemo(() => {
    if (!selectedId) return null;
    return JSON.stringify({
      name: editorName,
      category: editorCategory,
      legs: editorLegs,
    });
  }, [selectedId, editorName, editorCategory, editorLegs]);

  // Track the last payload we received from the backend so we don't
  // immediately PUT back the just-hydrated content.
  const lastSeenPayloadRef = useRef({ id: null, payload: null });

  useEffect(() => {
    if (!selectedId) {
      lastSeenPayloadRef.current = { id: null, payload: null };
      return;
    }
    if (lastSeenPayloadRef.current.id !== selectedId) {
      lastSeenPayloadRef.current = {
        id: selectedId,
        payload: JSON.stringify({
          name: editorName,
          category: editorCategory,
          legs: editorLegs,
        }),
      };
    }
    // We intentionally take a single snapshot per selection — subsequent
    // local edits change cloudPayload only, NOT lastSeenPayloadRef, so
    // cloudDirty flips true correctly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const cloudDirty = !!cloudPayload
    && (lastSeenPayloadRef.current.id !== selectedId
        || lastSeenPayloadRef.current.payload !== cloudPayload);
  cloudDirtyRef.current = cloudDirty;

  const handleCloudSave = useCallback(async (payloadStr, { signal } = {}) => {
    if (!selectedId || !payloadStr) return;
    const body = JSON.parse(payloadStr);
    try {
      await updateBasket(selectedId, body, { signal });
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      setCloudError(describePersistenceError(err));
      // eslint-disable-next-line no-console
      console.error('updateBasket (autosave) failed:', err);
      throw err;
    }
    if (signal && signal.aborted) return;
    setCloudError(null);
    lastSeenPayloadRef.current = { id: selectedId, payload: payloadStr };
    // Refresh the list so the sidebar reflects the latest name / category.
    fetchBaskets(category);
  }, [selectedId, category, fetchBaskets]);

  const {
    status: cloudStatus,
    reset: resetCloudStatus,
  } = useBackendAutosave({
    enabled: !!selectedId && cloudDirty,
    payload: cloudPayload,
    onSave: handleCloudSave,
  });

  // Reset cloud status whenever the selection changes.
  useEffect(() => {
    resetCloudStatus();
  }, [selectedId, resetCloudStatus]);

  // -------------------------------------------------------------------------
  // Mutations
  // -------------------------------------------------------------------------

  const handleCreate = useCallback(async () => {
    const id = newId.trim();
    const name = newName.trim();
    if (!id || !name) return;
    setOneshotStatus('saving');
    try {
      await createBasket({ id, name, category, legs: [] });
      setOneshotError(null);
      setOneshotStatus('saved');
      setNewId('');
      setNewName('');
      await fetchBaskets(category);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('createBasket failed:', err);
    }
  }, [newId, newName, category, fetchBaskets]);

  const handleArchive = useCallback(async () => {
    const id = archiveTarget;
    if (!id) return;
    setArchiveTarget(null);
    setOneshotStatus('saving');
    try {
      await archiveBasket(id);
      setOneshotError(null);
      setOneshotStatus('saved');
      if (selectedId === id) {
        setSelectedId(null);
        setEditorName('');
        setEditorLegs([]);
        resetCloudStatus();
      }
      await fetchBaskets(category);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('archiveBasket failed:', err);
    }
  }, [archiveTarget, selectedId, category, fetchBaskets, resetCloudStatus]);

  const handleChangeItemCat = useCallback(async (id, newCat) => {
    const target = baskets.find((b) => b.id === id);
    if (!target) return;
    setOneshotStatus('saving');
    try {
      await updateBasket(id, {
        name: target.name,
        category: newCat,
        legs: target.legs || [],
      });
      setOneshotError(null);
      setOneshotStatus('saved');
      // If the moved basket is the loaded one, sync the editor's
      // category too — otherwise the next autosave PUT would move it
      // back into the previous bucket.
      if (selectedId === id) setEditorCategory(newCat);
      await fetchBaskets(category);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('updateBasket (category change) failed:', err);
    }
  }, [baskets, selectedId, category, fetchBaskets]);

  const handleAddLeg = useCallback(() => {
    const iid = newLegId.trim();
    const coll = newLegCollection.trim();
    const w = parseFloat(newLegWeight);
    if (!iid || !coll || !Number.isFinite(w) || w === 0) return;
    if (editorLegs.some((l) => l.instrument_id === iid)) return;
    setEditorLegs((prev) => [...prev, { instrument_id: iid, collection: coll, weight: w }]);
    setNewLegId('');
    setNewLegWeight('');
  }, [newLegId, newLegCollection, newLegWeight, editorLegs]);

  const handleRemoveLeg = useCallback((instrument_id) => {
    setEditorLegs((prev) => prev.filter((l) => l.instrument_id !== instrument_id));
  }, []);

  // -------------------------------------------------------------------------
  // Status precedence — same rule as PortfolioPage: cloud 'saving' / 'error'
  // win over a stale one-shot 'saved'. Otherwise prefer one-shot.
  // -------------------------------------------------------------------------

  const displayedSaveStatus = (
    cloudStatus === 'saving' || cloudStatus === 'error'
      ? cloudStatus
      : (oneshotStatus !== 'idle' ? oneshotStatus : cloudStatus)
  );
  const saveErrorMessage = (
    displayedSaveStatus === 'error'
      ? (cloudStatus === 'error' ? cloudError : oneshotError)
      : null
  );

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const headerRight = (
    <div className={styles.headerActions}>
      <label className={styles.categoryLabel} htmlFor="basket-category-select">
        Category
      </label>
      <select
        id="basket-category-select"
        className={styles.categorySelect}
        value={category}
        onChange={(e) => setCategory(e.target.value)}
        aria-label="Filter baskets by category"
        data-testid="basket-category-filter"
      >
        {CATEGORIES.map((cat) => (
          <option key={cat} value={cat}>{cat}</option>
        ))}
      </select>
    </div>
  );

  return (
    <div className={styles.page}>
      <div className={styles.scroll}>
        <div className={styles.header}>
          <h2 className={styles.pageTitle}>Baskets</h2>
          {(oneshotStatus !== 'idle' || selectedId) && (
            <SaveStatus
              status={displayedSaveStatus}
              label="Cloud"
              errorMessage={displayedSaveStatus === 'error' ? saveErrorMessage : null}
            />
          )}
        </div>

        <div className={styles.body}>
          {/* ── Sidebar: list + create form ── */}
          <aside className={styles.sidePanel}>
            <Card
              title="Saved Baskets"
              right={headerRight}
              data-testid="persisted-basket-panel"
            >
              <div className={styles.createForm}>
                <input
                  className={styles.input}
                  placeholder="basket-id"
                  value={newId}
                  onChange={(e) => setNewId(e.target.value)}
                  data-testid="new-basket-id-input"
                />
                <input
                  className={styles.input}
                  placeholder="Basket name"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  data-testid="new-basket-name-input"
                />
                <button
                  type="button"
                  className={styles.createBtn}
                  onClick={handleCreate}
                  disabled={!newId.trim() || !newName.trim()}
                  data-testid="create-basket-btn"
                >
                  + Create
                </button>
              </div>
              <div className={styles.list}>
                {listLoading ? (
                  <div className={styles.empty}>Loading...</div>
                ) : listError ? (
                  <div className={styles.errorRow} data-testid="basket-list-error">
                    {listError}
                  </div>
                ) : baskets.length === 0 ? (
                  <div className={styles.empty} data-testid="basket-list-empty">
                    No saved baskets in {category} — create one above.
                  </div>
                ) : (
                  baskets.map((b) => {
                    const isSelected = b.id === selectedId;
                    return (
                      <div
                        key={b.id}
                        className={`${styles.row}${isSelected ? ` ${styles.rowActive}` : ''}`}
                        data-testid={`basket-row-${b.id}`}
                        data-selected={isSelected ? 'true' : 'false'}
                      >
                        <button
                          type="button"
                          className={styles.rowName}
                          onClick={() => handleSelect(b.id)}
                          title={`Load ${b.name}`}
                          data-testid={`load-basket-${b.id}`}
                        >
                          {b.name}
                        </button>
                        <select
                          className={styles.rowCatSelect}
                          value={b.category}
                          onChange={(e) => handleChangeItemCat(b.id, e.target.value)}
                          aria-label={`Category for ${b.name}`}
                          data-testid={`basket-cat-select-${b.id}`}
                          title="Move to category"
                        >
                          {CATEGORIES.map((cat) => (
                            <option key={cat} value={cat}>{cat}</option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className={styles.rowDeleteBtn}
                          onClick={() => setArchiveTarget(b.id)}
                          title="Archive basket"
                          aria-label={`Archive ${b.name}`}
                          data-testid={`archive-basket-${b.id}`}
                        >
                          ×
                        </button>
                      </div>
                    );
                  })
                )}
              </div>
            </Card>
            {oneshotStatus === 'error' && oneshotError && (
              <div className={styles.oneshotError} data-testid="basket-oneshot-error">
                {oneshotError}
              </div>
            )}
          </aside>

          {/* ── Editor ── */}
          <main className={styles.editorPanel}>
            {!selectedId && (
              <div className={styles.placeholder}>
                Select a basket from the list or create a new one.
              </div>
            )}
            {selectedId && editorLoading && (
              <div className={styles.placeholder}>Loading basket…</div>
            )}
            {selectedId && !editorLoading && (
              <>
                <Card title="Basket">
                  <div className={styles.editorHeader}>
                    <label className={styles.fieldLabel} htmlFor="basket-name-input">
                      Name
                    </label>
                    <input
                      id="basket-name-input"
                      className={styles.nameInput}
                      value={editorName}
                      onChange={(e) => setEditorName(e.target.value)}
                      data-testid="basket-name-input"
                    />
                  </div>
                </Card>

                <Card title="Legs">
                  {editorLegs.length === 0 ? (
                    <div className={styles.empty} data-testid="basket-legs-empty">
                      No legs yet — add one below.
                    </div>
                  ) : (
                    <table className={styles.legsTable}>
                      <thead>
                        <tr>
                          <th>Instrument ID</th>
                          <th>Collection</th>
                          <th>Weight</th>
                          <th aria-label="actions" />
                        </tr>
                      </thead>
                      <tbody>
                        {editorLegs.map((leg) => (
                          <tr key={leg.instrument_id} data-testid={`basket-leg-${leg.instrument_id}`}>
                            <td>{leg.instrument_id}</td>
                            <td>{leg.collection}</td>
                            <td>{leg.weight}</td>
                            <td>
                              <button
                                type="button"
                                className={styles.removeLegBtn}
                                onClick={() => handleRemoveLeg(leg.instrument_id)}
                                aria-label={`Remove ${leg.instrument_id}`}
                                data-testid={`remove-leg-${leg.instrument_id}`}
                              >
                                Remove
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}

                  <div className={styles.addLegForm}>
                    <input
                      className={styles.input}
                      placeholder="instrument_id"
                      value={newLegId}
                      onChange={(e) => setNewLegId(e.target.value)}
                      data-testid="new-leg-id"
                    />
                    <input
                      className={styles.input}
                      placeholder="collection"
                      value={newLegCollection}
                      onChange={(e) => setNewLegCollection(e.target.value)}
                      data-testid="new-leg-collection"
                    />
                    <input
                      className={styles.input}
                      type="number"
                      placeholder="weight"
                      value={newLegWeight}
                      step="0.01"
                      onChange={(e) => setNewLegWeight(e.target.value)}
                      data-testid="new-leg-weight"
                    />
                    <button
                      type="button"
                      className={styles.addLegBtn}
                      onClick={handleAddLeg}
                      disabled={
                        !newLegId.trim()
                        || !newLegCollection.trim()
                        || !Number.isFinite(parseFloat(newLegWeight))
                        || parseFloat(newLegWeight) === 0
                      }
                      data-testid="add-leg-btn"
                    >
                      Add leg
                    </button>
                  </div>
                </Card>
              </>
            )}
          </main>
        </div>
      </div>

      <ConfirmDialog
        open={archiveTarget !== null}
        title="Archive basket?"
        message={
          archiveTarget
            ? `Archive basket "${archiveTarget}"? It will move to ARCHIVE.`
            : ''
        }
        confirmLabel="Archive"
        cancelLabel="Cancel"
        destructive
        onConfirm={handleArchive}
        onCancel={() => setArchiveTarget(null)}
      />
    </div>
  );
}

export default BasketsPage;
