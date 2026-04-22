// Signals API helpers.
//
// Thin wrapper over ``POST /api/signals/compute``. Kept separate from the
// page code so components can mock the fetch in tests without stubbing
// ``globalThis.fetch`` directly.
//
// iter-4 response shape (PLAN.md § v3 contract):
//   {
//     timestamps: number[],                    // unix ms, union-aligned
//     positions: Array<{
//       input_id:   string,
//       instrument: {type: 'spot'|'continuous', ...},
//       values:       number[],                // length == timestamps.length
//       clipped_mask: boolean[],               // length == timestamps.length
//       price: {label: string, values: number[]} | null
//     }>,
//     indicators: Array<IndicatorTrace>,       // ALWAYS a list (iter-3 PROB-1)
//     clipped: boolean,                        // OR across all masks
//     diagnostics?: object
//   }
// Error envelope unchanged: {error_type, message, traceback?}.

/**
 * POST a signal-compute request and return the parsed response.
 *
 * On a non-2xx response the parsed JSON body is thrown as-is — callers
 * should treat it as the backend error envelope
 * ``{error_type, message, traceback?}``. Network errors propagate
 * untouched so the caller can classify via ``utils/fetchError``.
 *
 * @param {Object} spec
 *   the v4 Signal to evaluate — ``{id, name, inputs, rules: {entries,
 *   exits}}`` where each section is a list of Blocks
 *   ``{id, input_id, weight, conditions, target_entry_block_id?}``. See
 *   PLAN.md § "Wire contract (v4)".
 * @param {Array<{id: string, name: string, code: string, params: object, seriesMap: object}>} indicators
 *   list of every IndicatorSpec referenced by any operand in ``spec`` —
 *   always an array (iter-3 PROB-1).
 * @returns {Promise<Object>}
 *   the compute response with shape documented in the file header.
 */
export async function computeSignal(spec, indicators, { signal } = {}) {
  const res = await fetch('/api/signals/compute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      spec,
      indicators: indicators || [],
    }),
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    // Attach the HTTP status so the caller can decide on retry semantics.
    const err = new Error((body && body.message) || res.statusText || 'Request failed');
    err.body = body;
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/**
 * Walk a Signal spec and enumerate every indicator_id it references.
 *
 * Stable iteration order is not guaranteed — callers should treat the
 * output as a set. Used to (a) look up each indicator spec from the
 * Indicators localStorage and (b) validate that every referenced id
 * exists before firing the compute request.
 */
export function collectIndicatorIds(spec) {
  const out = new Set();
  if (!spec || !spec.rules) return out;
  const visitOperand = (op) => {
    if (!op || typeof op !== 'object') return;
    if (op.kind === 'indicator' && typeof op.indicator_id === 'string') {
      out.add(op.indicator_id);
    }
  };
  const visitCondition = (c) => {
    if (!c || typeof c !== 'object') return;
    // Discriminated by ``op`` — each variant has different operand fields.
    if (c.lhs) visitOperand(c.lhs);
    if (c.rhs) visitOperand(c.rhs);
    if (c.operand) visitOperand(c.operand);
    if (c.min) visitOperand(c.min);
    if (c.max) visitOperand(c.max);
  };
  for (const dir of Object.keys(spec.rules)) {
    const blocks = spec.rules[dir] || [];
    for (const block of blocks) {
      const conds = (block && block.conditions) || [];
      for (const c of conds) visitCondition(c);
    }
  }
  return out;
}
