// Market data source (v1 = tcg_instruments, v2 = tcg_instruments_v2) — the
// single source of truth for the value domain, the wire encoding, and the
// user-facing capability notes.
//
// WIRE CONTRACT: a top-level ``data_source`` string field on the
// /portfolio/compute and /signals/compute bodies, ``"v1"`` | ``"v2"``, backend
// default ``"v1"``. Emitted ONLY when it is "v2" (see
// ``dataSourceFieldsForRequest``) so a default request stays byte-identical to a
// pre-feature payload — the backend keys its result cache off the hashed body,
// so an unconditional ``data_source:"v1"`` would invalidate every existing
// cache entry. Same idiom as ``costFieldsForRequest`` (PR#84).

export const DATA_SOURCE_V1 = 'v1';
export const DATA_SOURCE_V2 = 'v2';
export const DEFAULT_DATA_SOURCE = DATA_SOURCE_V1;

export const DATA_SOURCE_OPTIONS = [
  { value: DATA_SOURCE_V1, label: 'Database v1' },
  { value: DATA_SOURCE_V2, label: 'Database v2' },
];

/**
 * Coerce an arbitrary value to a valid data source. Anything that is not
 * exactly ``"v2"`` collapses to the v1 default — a corrupt/absent value can
 * never silently route a run to v2.
 *
 * @param {*} raw
 * @returns {'v1'|'v2'}
 */
export function coerceDataSource(raw) {
  return raw === DATA_SOURCE_V2 ? DATA_SOURCE_V2 : DATA_SOURCE_V1;
}

/**
 * Build the optional ``data_source`` request field.
 *
 * The key is emitted ONLY for v2; v1 (the default) yields ``{}`` so the body is
 * byte-identical to a pre-feature payload. Shared by the signal and portfolio
 * body builders AND the api/ wrappers so every path encodes it identically
 * (one helper, no drift) — a mismatch would key the backend result cache
 * differently between Compute, the cache-status probe and the cache-get.
 *
 * @param {*} dataSource
 * @returns {{data_source?: 'v2'}}
 */
export function dataSourceFieldsForRequest(dataSource) {
  return dataSource === DATA_SOURCE_V2 ? { data_source: DATA_SOURCE_V2 } : {};
}

// Measured v2 capability limits (live-probed 2026-07-27). Shown next to the
// selector whenever v2 is active so a run that is about to fail — or a
// comparison whose tails diverge for a trivial reason — is visible BEFORE the
// user clicks Compute/Run, rather than as a raw backend 400.
export const V2_LIMITATIONS = [
  'Coverage is S&P 500 only: IND_SP_500, FUT_SP_500 and weekly ES options (OPT_SP_500, cycles W1–W4 Friday). Any other instrument errors.',
  'Monthly (M) option cycles do not exist in v2, and an option leg with no cycle filter errors.',
  'The mid option stream is unavailable (settlement only, no quotes) — use close or bs_mid.',
  'History floors: options from 2011 (EW1/EW2/EW4) and 2016-02-22 (EW3); futures settlement from 2010-06-07. v1 reaches further back.',
  'v1 ends 2026-06-10 (futures) / 2026-06-11 (index) / 2026-06-12 (options) while v2 runs to 2026-07-21 — end both runs at 2026-06-10 so no leg runs past its v1 data, or the tails diverge for a trivial reason.',
  'On v2 futures, open/high/low/volume are often NaN (only ~40.6% of settlement days have a matching daily bar).',
];