// Single source of the /portfolio/compute wire body.
//
// Extracted from usePortfolio.handleCalculate so the CACHE-KEY path (badge) and
// the COMPUTE path build the exact same object — key parity is a hard guardrail.
// This function is pure: same (legs, rebalance, start, end, availableIndicators)
// in → same body out. It performs NO validation side effects; it surfaces the
// set of missing indicator ids so the caller can decide (compute aborts; the
// badge treats a missing-indicator body as un-keyable).

import { buildComputeRequestBody } from '../Signals/requestBuilder';

/**
 * Build the resolved compute request body.
 *
 * @returns {{ body, missing: string[], missingByLeg: {label, ids}[] }}
 *   ``body`` = {legs, weights, rebalance, return_type, start, end}, mirroring
 *   exactly what api/portfolio.computePortfolio sends (``start``/``end`` collapse
 *   falsy → undefined, dropped by JSON). ``missing`` = the de-duped set of
 *   indicator ids referenced by signal legs but absent from
 *   ``availableIndicators``. ``missingByLeg`` = per-leg detail (leg order
 *   preserved) so the caller can reproduce the original per-leg error message.
 */
export function buildPortfolioComputeBody({ legs, rebalance, start, end, availableIndicators }) {
  const apiLegs = {};
  const missing = [];
  const missingByLeg = [];

  for (const leg of legs) {
    if (leg.type === 'signal') {
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
    },
    missing: [...new Set(missing)],
    missingByLeg,
  };
}
