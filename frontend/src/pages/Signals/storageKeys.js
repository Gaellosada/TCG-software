// Centralised localStorage key constants for the Signals page.
//
// All keys share the ``tcg.signals`` namespace so collisions with the
// Indicators page (``tcg.indicators.*``) are impossible. A future
// key-version migration can be done in one place.
//
// v5: adds option_stream to the InputInstrument union. Clean break from v4.
// Pre-v5 payloads are discarded on load.
// v6: exit blocks gain a plural ``target_entry_block_names`` array (an exit
// may close several entries). The localStorage KEY stays ``tcg.signals.v5``
// — v5 payloads are migrated IN PLACE (the in-payload ``version`` field
// flips 5 → 6) so existing drafts survive; see storage.js migrateV5ToV6.

/** Versioned schema key for the main signals state (signals[]). */
export const SIGNALS_STORAGE_KEY = 'tcg.signals.v5';

/** Autosave-enabled toggle persisted per browser session. */
export const AUTOSAVE_KEY = 'tcg.signals.autosave';
