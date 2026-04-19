// Pure helpers for building a signal-compute request body.
//
// Kept separate from ``SignalsPage.jsx`` so that unit tests can import
// them without pulling the Plotly/CodeMirror dependency tree into the
// test env.

import { collectIndicatorIds } from '../../api/signals';

/**
 * Build the backend request body for a signal.
 *
 * @param {Object} signal               the spec in its localStorage shape
 * @param {Array}  availableIndicators  indicator specs hydrated from the
 *                                      Indicators localStorage
 *                                      (``{id, name, code, params, seriesMap}``)
 * @returns {{body: Object, missing: string[]}}
 *   ``body`` — the literal POST body
 *   ``missing`` — indicator_ids that were referenced but absent from the
 *                 available-indicators array; callers should abort the
 *                 request and surface a validation error.
 */
export function buildComputeRequestBody(signal, availableIndicators) {
  const needed = collectIndicatorIds(signal);
  const indicatorMap = {};
  const missing = [];
  for (const id of needed) {
    const ind = (availableIndicators || []).find((i) => i.id === id);
    if (!ind) {
      missing.push(id);
      continue;
    }
    indicatorMap[id] = {
      code: ind.code,
      params: ind.params,
      seriesMap: ind.seriesMap,
    };
  }
  return {
    body: {
      spec: signal,
      indicators: indicatorMap,
      instruments: {},
    },
    missing,
  };
}
