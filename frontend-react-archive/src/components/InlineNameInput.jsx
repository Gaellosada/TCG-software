import { useState, useEffect, useRef } from 'react';

/**
 * Inline name input used in the editor-panel header of the Indicators
 * and Signals pages. Uses a local draft so typing does not rerun the
 * whole page on each keystroke — the committed name propagates to the
 * parent on blur or Enter.
 *
 * Extracted from two near-identical copies previously inlined in
 * IndicatorsPage.jsx (``IndicatorNameInput``) and
 * SignalsPage.jsx (``SignalNameInput``). The only material difference
 * between the two was copy strings and the ``readonly`` handling for
 * default indicators; both are accepted via props here.
 *
 * Props:
 *   - ``entity``: the object carrying ``{id, name, readonly?}``. null =
 *     nothing selected.
 *   - ``onRename(id, newName)``: committed-value callback.
 *   - ``className``: class applied to the <input> (page-specific styling).
 *   - ``placeholder``: string shown when ``entity`` is null.
 *   - ``selectedPlaceholder``: string shown when an entity is selected.
 *   - ``ariaLabel``: aria-label on the <input>.
 *   - ``title`` (optional): title attribute resolver. Either a string
 *     or a function ``(entity) => string`` (to differentiate readonly
 *     vs editable titles, as Indicators does).
 */
function InlineNameInput({
  entity,
  onRename,
  className,
  placeholder,
  selectedPlaceholder,
  ariaLabel,
  title,
}) {
  const [draft, setDraft] = useState(entity?.name || '');
  const prevIdRef = useRef(entity?.id);
  // Tracks whether the input currently has focus. We flip it in the
  // focus/blur handlers and consult it in the reset effect so external
  // renames (e.g. switching selection) don't stomp a user's in-progress
  // edit. A ref (not state) — toggling it must not trigger a rerender.
  const focusedRef = useRef(false);

  // Reset draft whenever the selected entity changes.
  useEffect(() => {
    if (prevIdRef.current !== entity?.id) {
      prevIdRef.current = entity?.id;
      setDraft(entity?.name || '');
    } else if ((entity?.name || '') !== draft && !focusedRef.current) {
      // External rename (e.g. defaults) — sync when the input is not focused.
      setDraft(entity?.name || '');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entity?.id, entity?.name]);

  const readonly = !entity || !!entity?.readonly;

  function commit() {
    focusedRef.current = false;
    if (!entity || readonly) {
      setDraft(entity?.name || '');
      return;
    }
    const next = draft.trim();
    if (!next || next === entity.name) {
      setDraft(entity.name);
      return;
    }
    if (onRename) onRename(entity.id, next);
  }

  const resolvedTitle = typeof title === 'function' ? title(entity) : title;

  return (
    <input
      className={className}
      type="text"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onFocus={() => { focusedRef.current = true; }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur(); }
      }}
      disabled={readonly}
      placeholder={entity ? selectedPlaceholder : placeholder}
      aria-label={ariaLabel}
      {...(resolvedTitle !== undefined ? { title: resolvedTitle } : {})}
    />
  );
}

export default InlineNameInput;
