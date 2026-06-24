/**
 * Centralised TanStack Query key factory.
 *
 * Every query/mutation in the app refers to its cache entry through one of
 * these factories so keys stay consistent between the reader (useQuery) and
 * the invalidator (queryClient.invalidateQueries). A typo'd inline key array
 * silently creates a *different* cache entry — funnelling every key through
 * this module makes that class of bug impossible.
 *
 * Key design:
 *   - Market-data keys are namespaced under 'market' so a single
 *     invalidateQueries({ queryKey: ['market'] }) can blow the whole
 *     slow-changing cache if ever needed (e.g. after a warehouse refresh),
 *     without touching persistence lists.
 *   - Persistence keys are namespaced under 'persistence' and sub-grouped by
 *     resource so a mutation invalidates exactly its own list/detail and
 *     nothing else.
 *   - Params are appended in a STABLE order. TanStack hashes keys
 *     deterministically (object keys are sorted internally), but arrays are
 *     positional — so the positional order here is the contract.
 *
 * Returned arrays are the literal query keys; spread/extend only via these
 * helpers, never hand-roll an array at a call site.
 */

export const queryKeys = {
  // ── Market data (slow-changing; long staleTime) ────────────────────────
  market: {
    /** all market-data queries — coarse invalidation root */
    all: () => ['market'],

    /** GET /data/collections (optionally filtered by asset class) */
    collections: (assetClass = null) => ['market', 'collections', assetClass ?? null],

    /** GET /data/{collection}?skip&limit — paginated instrument list */
    instruments: (collection, skip = 0, limit = 50) =>
      ['market', 'instruments', collection, skip, limit],

    /** GET /data/{collection}/{instrument} — price series */
    prices: (collection, instrument) => ['market', 'prices', collection, instrument],

    /**
     * GET /data/continuous/{collection} — rolled continuous series.
     * ``cycle`` is normalised: ''/undefined/null all collapse to null (the
     * "all cycles" case) so the Data-page chart and a portfolio leg with no
     * cycle hit the SAME cache entry. ``rollOffset`` is coerced to a number
     * for the same reason.
     */
    continuous: (collection, { strategy, adjustment, cycle, rollOffset } = {}) => [
      'market',
      'continuous',
      collection,
      strategy || 'front_month',
      adjustment || 'none',
      cycle || null,
      Number(rollOffset) || 0,
    ],

    /** GET /data/continuous/{collection}/cycles — available roll cycles */
    cycles: (collection) => ['market', 'cycles', collection],

    /** GET /options/roots — option-root catalogue (used by CategoryBrowser + charts) */
    optionRoots: () => ['market', 'optionRoots'],

    /** GET /options/expirations?root= — distinct expirations for a root */
    optionExpirations: (root) => ['market', 'optionExpirations', root],

    /** GET /options/contract/{coll}/{id} — per-contract time series */
    optionContract: (collection, contractId, { computeMissing, dateFrom, dateTo } = {}) => [
      'market',
      'optionContract',
      collection,
      contractId,
      computeMissing ?? null,
      dateFrom ?? null,
      dateTo ?? null,
    ],

    /** GET /options/chain-snapshot — single-date IV/delta smile snapshot */
    chainSnapshot: (root, { date, type, expiration, field, expiration_cycle } = {}) => [
      'market',
      'chainSnapshot',
      root,
      date ?? null,
      type ?? null,
      expiration ?? null,
      field ?? null,
      expiration_cycle ?? null,
    ],

    /**
     * Composite key for the CategoryBrowser sidebar load (collections +
     * per-collection instruments + option roots fanned out in one queryFn).
     * Kept distinct from the granular keys above because it represents the
     * orchestrated sidebar payload, not any single endpoint.
     */
    categoryBrowser: () => ['market', 'categoryBrowser'],
  },

  // ── Persistence (user-mutable; invalidated on edit) ────────────────────
  persistence: {
    /** all persistence queries — coarse invalidation root */
    all: () => ['persistence'],

    signals: {
      list: (category) => ['persistence', 'signals', 'list', category],
      detail: (id) => ['persistence', 'signals', 'detail', id],
    },
    portfolios: {
      list: (category) => ['persistence', 'portfolios', 'list', category],
      detail: (id) => ['persistence', 'portfolios', 'detail', id],
    },
    indicators: {
      // Indicators use a flat list (no category) — see api/persistence.js.
      list: () => ['persistence', 'indicators', 'list'],
      detail: (id) => ['persistence', 'indicators', 'detail', id],
    },
    baskets: {
      list: (category) => ['persistence', 'baskets', 'list', category],
      detail: (id) => ['persistence', 'baskets', 'detail', id],
    },
    tickets: {
      // Tickets use a flat list (no category) — a ticket is a single
      // free-text note (see api/tickets.js). The self-contained backend
      // path means there is no per-id detail query; only the list.
      list: () => ['persistence', 'tickets', 'list'],
    },
  },
};

export default queryKeys;
