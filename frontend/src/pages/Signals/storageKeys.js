// Centralised localStorage key constants for the Signals page.
//
// All keys share the ``tcg.signals`` namespace so collisions with the
// Indicators page (``tcg.indicators.*``) are impossible. A future
// key-version migration can be done in one place.

// NOTE (iter-4): schema bumped v2 → v3 when named ``inputs`` became
// first-class. Blocks carry ``input_id`` (no more embedded instrument);
// operands carry ``input_id``. v2 payloads are deliberately NOT migrated
// — the architectural shift means any v2 signal needs a fresh input
// declaration. ``loadState`` discards any ``version !== 3`` payload and
// emits a single console.warn.

/** Versioned schema key for the main signals state (signals[]). */
export const SIGNALS_STORAGE_KEY = 'tcg.signals.v3';

/** Autosave-enabled toggle persisted per browser session. */
export const AUTOSAVE_KEY = 'tcg.signals.autosave';
