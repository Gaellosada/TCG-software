// Centralised localStorage key constants for the Signals page.
//
// All keys share the ``tcg.signals`` namespace so collisions with the
// Indicators page (``tcg.indicators.*``) are impossible. A future
// key-version migration can be done in one place.
//
// v5: adds option_stream to the InputInstrument union. Clean break from v4.
// Pre-v5 payloads are discarded on load — no migration code.

/** Versioned schema key for the main signals state (signals[]). */
export const SIGNALS_STORAGE_KEY = 'tcg.signals.v5';

/** Autosave-enabled toggle persisted per browser session. */
export const AUTOSAVE_KEY = 'tcg.signals.autosave';
