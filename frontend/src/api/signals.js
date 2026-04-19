// Signals API helpers.
//
// Thin wrapper over ``POST /api/signals/compute``. Kept separate from the
// page code so components can mock the fetch in tests without stubbing
// ``globalThis.fetch`` directly.

/**
 * POST a signal-compute request and return the parsed response.
 *
 * On a non-2xx response the parsed JSON body is thrown as-is — callers
 * should treat it as the backend error envelope
 * ``{error_type, message, traceback?}``. Network errors propagate
 * untouched so the caller can classify via ``utils/fetchError``.
 *
 * @param {Object} spec         the Signal to evaluate (see PLAN.md § Contract)
 * @param {Object} indicators   map of indicator_id → {code, params, seriesMap}
 * @returns {Promise<Object>}   the compute response
 */
export async function computeSignal(spec, indicators) {
  const res = await fetch('/api/signals/compute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      spec,
      indicators: indicators || {},
      instruments: {},
    }),
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
