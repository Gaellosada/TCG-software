// Single source of the /portfolio/compute wire body.
//
// Shared by the COMPUTE path (usePortfolio.handleCalculate) and the cache-STATUS
// probe (usePortfolioCacheStatus) so both build the exact same object — the
// backend keys its result cache off this body, so a status match means an
// identical Compute is served from cache. This function is pure: same (legs,
// rebalance, start, end, availableIndicators, resolvePortfolio) in → same body
// out. It performs NO validation side effects; it surfaces the set of missing
// indicator ids + broken portfolio refs so the caller can decide (compute
// aborts; the status probe treats such a body as un-keyable).

import { buildComputeRequestBody, costFieldsForRequest } from '../Signals/requestBuilder';
import { persistedDocToLegs } from './persistedDoc';

/**
 * Build the resolved compute request body.
 *
 * @param {object}   p
 * @param {Array}    p.legs
 * @param {string}   p.rebalance
 * @param {string=}  p.start
 * @param {string=}  p.end
 * @param {Array=}   p.availableIndicators
 * @param {(id: string) => (object|null)=} p.resolvePortfolio
 *   Resolver for ``type:"portfolio"`` (composed) legs: given a child portfolio
 *   id, returns the child's CURRENT saved doc ({legs, rebalance, ...}) or null
 *   if it can't be resolved (deleted / archived / empty → broken reference).
 *   The child spec is inlined FRESH at build time (never snapshotted on the
 *   leg), so a content-addressed cache key over this body busts automatically
 *   when the child is edited — the live-reference mechanism (design §4).
 *   Optional: when omitted (pure page / legacy callers) a ``portfolio`` leg is
 *   reported as a broken reference and NOT emitted.
 * @param {number=}  _depth  Internal recursion depth. Depth-1 is enforced here
 *   (belt to the backend's suspenders): a ``portfolio`` leg encountered inside a
 *   child (``_depth >= 1``) is a broken reference, never inlined — the graph is
 *   acyclic by construction.
 *
 * @returns {{ body, missing: string[], missingByLeg: {label, ids}[],
 *             brokenRefs: {label, portfolioId, reason}[] }}
 *   ``body`` = {legs, weights, rebalance, return_type, start, end}, mirroring
 *   exactly what api/portfolio.computePortfolio sends (``start``/``end`` collapse
 *   falsy → undefined, dropped by JSON). ``missing`` = the de-duped set of
 *   indicator ids referenced by signal legs but absent from
 *   ``availableIndicators``. ``missingByLeg`` = per-leg detail (leg order
 *   preserved) so the caller can reproduce the original per-leg error message.
 *   ``brokenRefs`` = composed legs whose child could not be resolved; the caller
 *   blocks compute and the UI badges them (design §5).
 */
export function buildPortfolioComputeBody({
  legs,
  rebalance,
  start,
  end,
  availableIndicators,
  resolvePortfolio,
  // Global execution costs (basis points). Added to the TOP-LEVEL body only
  // (see costFieldsForRequest — present only when > 0). NOT threaded into the
  // recursive child call: slippage/fees are a single global field applied once
  // by the top-level body, and the inlined child object carries only its own
  // legs/weights/rebalance/return_type. Must be passed identically by the
  // compute path AND the cache-status probe so the backend cache key matches.
  slippageBps,
  feesBps,
  _depth = 0,
}) {
  const apiLegs = {};
  const missing = [];
  const missingByLeg = [];
  const brokenRefs = [];

  for (const leg of legs) {
    if (leg.type === 'portfolio') {
      // Composed leg: resolve the child's CURRENT spec and inline it under
      // ``portfolio`` (symmetric with the top-level body; mirrors how a signal
      // leg inlines ``signal_spec``). Depth-1: a portfolio leg inside a child is
      // NOT inlined (broken ref) so recursion terminates and the graph is
      // acyclic. Do NOT emit a leg for a broken/depth-exceeded ref — an empty
      // ``portfolio`` would trip the backend's own empty-child 400; the caller
      // blocks compute on ``brokenRefs`` first, well before that.
      const portfolioId = leg.portfolioId || leg.portfolio_id || null;
      if (_depth >= 1) {
        brokenRefs.push({ label: leg.label, portfolioId, reason: 'depth' });
        continue;
      }
      const childDoc = typeof resolvePortfolio === 'function'
        ? resolvePortfolio(portfolioId)
        : null;
      if (!childDoc) {
        brokenRefs.push({ label: leg.label, portfolioId, reason: 'unresolved' });
        continue;
      }
      // Convert the child's persisted legs (same converter loadFromPersisted
      // uses) and recurse through the SAME builder — the child sub-body is built
      // identically to a top-level one, so its inlined shape is byte-stable and
      // the cache key captures every child field.
      const childLegs = persistedDocToLegs(childDoc);
      const childBuilt = buildPortfolioComputeBody({
        legs: childLegs,
        rebalance: typeof childDoc.rebalance === 'string' ? childDoc.rebalance : 'none',
        // The nested ``portfolio`` object carries NO start/end (design §4) — the
        // parent's window drives the shared date grid.
        start: undefined,
        end: undefined,
        availableIndicators,
        resolvePortfolio,
        _depth: _depth + 1,
      });
      // Propagate the child's own diagnostics so the parent surfaces them.
      if (childBuilt.missing.length > 0) missing.push(...childBuilt.missing);
      for (const m of childBuilt.missingByLeg) {
        missingByLeg.push({ label: `${leg.label} › ${m.label}`, ids: m.ids });
      }
      for (const b of childBuilt.brokenRefs) {
        brokenRefs.push({ ...b, label: `${leg.label} › ${b.label}` });
      }
      apiLegs[leg.label] = {
        type: 'portfolio',
        portfolio_id: portfolioId,   // provenance only; backend inlines, not loads
        portfolio: {
          legs: childBuilt.body.legs,
          weights: childBuilt.body.weights,
          rebalance: childBuilt.body.rebalance,
          return_type: childBuilt.body.return_type,
        },
      };
    } else if (leg.type === 'signal') {
      const { body, missing: legMissing } = buildComputeRequestBody(
        leg.signalSpec,
        availableIndicators,
      );
      if (legMissing && legMissing.length > 0) {
        missing.push(...legMissing);
        missingByLeg.push({ label: leg.label, ids: legMissing });
      }
      apiLegs[leg.label] = {
        type: 'signal',
        signal_spec: body,
      };
    } else if (leg.type === 'option_stream') {
      apiLegs[leg.label] = {
        type: 'option_stream',
        collection: leg.collection,
        option_type: leg.option_type,
        cycle: leg.cycle,
        maturity: leg.maturity,
        selection: leg.selection,
        stream: leg.stream,
      };
      // An option PRICE leg (mid/bs_mid) is hold-ON-only; always send hold for a
      // premium leg (covers legacy legs too). Level streams (iv/greeks) never hold.
      const isPremiumLeg = leg.stream === 'mid' || leg.stream === 'bs_mid';
      if (isPremiumLeg || leg.hold_between_rolls) {
        apiLegs[leg.label].hold_between_rolls = true;
        apiLegs[leg.label].nav_times = leg.nav_times ?? 1.0;
        if (leg.sizing_mode === 'futures_notional') {
          apiLegs[leg.label].sizing_mode = 'futures_notional';
          apiLegs[leg.label].futures_reference =
            leg.futures_reference || 'nearest_on_or_after';
        }
      }
      const ro = leg.roll_offset;
      if (ro && typeof ro === 'object' && ro.value > 0) {
        apiLegs[leg.label].roll_offset = { value: ro.value, unit: ro.unit || 'days' };
      } else if (typeof ro === 'number' && ro > 0) {
        apiLegs[leg.label].roll_offset = { value: ro, unit: 'days' };
      }
    } else if (leg.type === 'continuous') {
      apiLegs[leg.label] = {
        type: 'continuous',
        collection: leg.collection,
        strategy: leg.strategy || 'front_month',
        adjustment: leg.adjustment || 'none',
      };
      if (leg.cycle) {
        apiLegs[leg.label].cycle = leg.cycle;
      }
      if (leg.rollOffset > 0) {
        apiLegs[leg.label].roll_offset = leg.rollOffset;
      }
      if (leg.rank > 1) {
        apiLegs[leg.label].rank = leg.rank;
      }
    } else {
      apiLegs[leg.label] = {
        type: 'instrument',
        collection: leg.collection,
        symbol: leg.symbol,
      };
    }
  }

  const apiWeights = {};
  for (const leg of legs) {
    // A broken/depth-exceeded portfolio leg emits no entry in ``apiLegs`` — do
    // NOT emit a dangling weight for it either (label→weight must key exactly
    // the emitted legs). Every non-portfolio leg is always emitted, so this
    // guard is a no-op on the existing paths (byte-identical output).
    if (!(leg.label in apiLegs)) continue;
    apiWeights[leg.label] = Number(leg.weight) || 0;
  }

  return {
    body: {
      legs: apiLegs,
      weights: apiWeights,
      rebalance,
      return_type: 'normal',
      start: start || undefined,
      end: end || undefined,
      // Global execution costs — present only when > 0, and only at the
      // top level (_depth 0). Recursive child bodies never receive costs.
      ...(_depth === 0 ? costFieldsForRequest({ slippageBps, feesBps }) : {}),
    },
    missing: [...new Set(missing)],
    missingByLeg,
    brokenRefs,
  };
}
