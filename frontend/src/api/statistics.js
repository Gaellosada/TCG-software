// Statistics API helper.
//
// Thin wrapper over ``POST /api/statistics``. Kept separate from the
// component so tests can mock the fetch without stubbing
// ``globalThis.fetch`` directly.
//
// Locked contract (Wave 1):
//   Request  : {dates: number[], equity: number[], risk_free_rate: number}
//   Response : {
//     return: {total_return, cagr, annualized_volatility,
//              best_day, worst_day, best_month, worst_month},
//     risk_adjusted: {sharpe_ratio, sortino_ratio, calmar_ratio},
//     tail: {var_95, var_99, cvar_5, skewness, kurtosis},   // skew/kurt may be null
//     drawdown: {max_drawdown, avg_drawdown, current_drawdown,
//                longest_drawdown_days, time_underwater_days},
//     risk_free_rate_used: number,
//     num_observations: number,
//   }
// Error envelope mirrors signals.js: parsed JSON body is rethrown with
// ``.body``/``.status`` attached.

/**
 * POST a statistics request and return the parsed response.
 *
 * @param {Object}   payload
 * @param {number[]} payload.dates          YYYYMMDD integers, length == equity.length
 * @param {number[]} payload.equity         equity curve values
 * @param {number}   payload.riskFreeRate   annualized decimal rate, e.g. 0.04 for 4%
 * @param {Object}   [opts]
 * @param {AbortSignal} [opts.signal]       fetch abort signal
 * @returns {Promise<Object>}               the statistics suite, shape above
 */
export async function fetchStatistics({ dates, equity, riskFreeRate }, { signal } = {}) {
  const res = await fetch('/api/statistics', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      dates,
      equity,
      risk_free_rate: riskFreeRate,
    }),
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const err = new Error((body && body.message) || res.statusText || 'Request failed');
    err.body = body;
    err.status = res.status;
    throw err;
  }
  return res.json();
}
