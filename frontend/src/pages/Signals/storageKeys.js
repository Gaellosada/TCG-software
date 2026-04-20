// Centralised localStorage key constants for the Signals page.
//
// All keys share the ``tcg.signals`` namespace so collisions with the
// Indicators page (``tcg.indicators.*``) are impossible. A future
// key-version migration can be done in one place.

// NOTE (iter-3): schema bumped v1 → v2 when per-block ``instrument`` and
// ``weight`` fields were introduced. v1 payloads are deliberately not
// migrated — there is only one demo signal in the wild and the old shape
// cannot express a weighted multi-instrument allocation. ``loadState``
// discards any ``version !== 2`` payload and emits a single console.warn.

/** Versioned schema key for the main signals state (signals[]). */
export const SIGNALS_STORAGE_KEY = 'tcg.signals.v2';

/** Autosave-enabled toggle persisted per browser session. */
export const AUTOSAVE_KEY = 'tcg.signals.autosave';
