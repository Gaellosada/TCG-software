// Centralised localStorage key constants for the Signals page.
//
// All keys share the ``tcg.signals`` namespace so collisions with the
// Indicators page (``tcg.indicators.*``) are impossible. A future
// key-version migration can be done in one place.

/** Versioned schema key for the main signals state (signals[]). */
export const SIGNALS_STORAGE_KEY = 'tcg.signals.v1';

/** Autosave-enabled toggle persisted per browser session. */
export const AUTOSAVE_KEY = 'tcg.signals.autosave';
