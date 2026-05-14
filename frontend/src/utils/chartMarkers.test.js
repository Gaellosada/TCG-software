// @vitest-environment jsdom
//
// Tests for chartMarkers — the shared marker-trace producer.
//
// Invariants pinned here:
//   - hovertemplate distinguishes sell ("Close") vs buy ("Open")
//   - all 5 customdata fields are referenced in the template
//   - empty/unknown input returns null (single trace) or [] (all traces)
//   - sell uses circle-open, buy uses circle (declarative MARKER_STYLE)
//   - color comes from the theme palette (not hardcoded)
//   - legend wiring: legendgroup + showlegend on every produced trace

import { describe, it, expect } from 'vitest';
import {
  buildMarkerHovertemplate,
  buildMarkerTrace,
  buildAllMarkerTraces,
} from './chartMarkers';
import { getChartColors } from './chartTheme';

function makeMarker(kind, overrides = {}) {
  return {
    x: '2024-03-15',
    y: 12.35,
    kind,
    tooltip: {
      contract_id: `OPT_${kind}_id`,
      root: 'IND_SP_500',
      expiration: '2024-04-19',
      strike: 4500.0,
      type: 'C',
      value: 12.35,
    },
    ...overrides,
  };
}

describe('buildMarkerHovertemplate', () => {
  it('returns a string starting with "<b>Sell</b>" for sell kind', () => {
    const tpl = buildMarkerHovertemplate('sell');
    expect(typeof tpl).toBe('string');
    expect(tpl.startsWith('<b>Sell</b>')).toBe(true);
  });

  it('returns a string starting with "<b>Buy</b>" for buy kind', () => {
    const tpl = buildMarkerHovertemplate('buy');
    expect(typeof tpl).toBe('string');
    expect(tpl.startsWith('<b>Buy</b>')).toBe(true);
  });

  it('references all 5 customdata indices (root, expiration, strike, type, value)', () => {
    for (const kind of ['sell', 'buy']) {
      const tpl = buildMarkerHovertemplate(kind);
      expect(tpl).toContain('%{customdata[0]}');
      expect(tpl).toContain('%{customdata[1]}');
      expect(tpl).toContain('%{customdata[2]}');
      expect(tpl).toContain('%{customdata[3]}');
      expect(tpl).toContain('%{customdata[4]');
    }
  });

  it('uses the <extra></extra> sentinel to suppress the default trace box', () => {
    expect(buildMarkerHovertemplate('sell')).toContain('<extra></extra>');
    expect(buildMarkerHovertemplate('buy')).toContain('<extra></extra>');
  });
});

describe('buildMarkerTrace', () => {
  it('returns null when markersOfKind is empty', () => {
    expect(buildMarkerTrace([], 'sell', 'dark')).toBeNull();
    expect(buildMarkerTrace([], 'buy', 'light')).toBeNull();
  });

  it('returns null when markersOfKind is undefined or null', () => {
    expect(buildMarkerTrace(undefined, 'sell', 'dark')).toBeNull();
    expect(buildMarkerTrace(null, 'buy', 'dark')).toBeNull();
  });

  it('returns null for an unknown kind', () => {
    expect(buildMarkerTrace([makeMarker('sell')], 'mystery', 'dark')).toBeNull();
  });

  it('produces a circle-open trace for sell markers', () => {
    const trace = buildMarkerTrace([makeMarker('sell')], 'sell', 'dark');
    expect(trace).not.toBeNull();
    expect(trace.marker.symbol).toBe('circle-open');
    expect(trace.marker.size).toBe(8);
    expect(trace.marker.line.width).toBe(1.5);
    expect(trace.name).toBe('Roll — sell');
  });

  it('produces a filled circle trace for buy markers', () => {
    const trace = buildMarkerTrace([makeMarker('buy')], 'buy', 'dark');
    expect(trace).not.toBeNull();
    expect(trace.marker.symbol).toBe('circle');
    expect(trace.marker.size).toBe(8);
    expect(trace.marker.line.width).toBe(0);
    expect(trace.name).toBe('Roll — buy');
  });

  it('emits legend wiring (legendgroup + showlegend) on every trace', () => {
    for (const kind of ['sell', 'buy']) {
      const t = buildMarkerTrace([makeMarker(kind)], kind, 'dark');
      expect(t.legendgroup).toBe('roll-markers');
      expect(t.showlegend).toBe(true);
    }
  });

  it('opts out of CSV export via meta.skipCsv (overlay, not user data)', () => {
    // Pinned contract: chartCsv.js#isExportable filters out any trace with
    // meta.skipCsv === true. Marker traces are a visualization overlay, not
    // data the user wants in a CSV download.
    for (const kind of ['sell', 'buy']) {
      const t = buildMarkerTrace([makeMarker(kind)], kind, 'dark');
      expect(t.meta).toEqual({ skipCsv: true });
    }
  });

  it('pulls marker colors from the theme palette (dark)', () => {
    const colors = getChartColors('dark');
    const sell = buildMarkerTrace([makeMarker('sell')], 'sell', 'dark');
    const buy = buildMarkerTrace([makeMarker('buy')], 'buy', 'dark');
    expect(sell.marker.color).toBe(colors.markerSell);
    expect(sell.marker.line.color).toBe(colors.markerSell);
    expect(buy.marker.color).toBe(colors.markerBuy);
    expect(buy.marker.line.color).toBe(colors.markerBuy);
  });

  it('pulls marker colors from the theme palette (light)', () => {
    const colors = getChartColors('light');
    const sell = buildMarkerTrace([makeMarker('sell')], 'sell', 'light');
    const buy = buildMarkerTrace([makeMarker('buy')], 'buy', 'light');
    expect(sell.marker.color).toBe(colors.markerSell);
    expect(buy.marker.color).toBe(colors.markerBuy);
    // Dark and light palettes differ — make sure we are not silently
    // using the dark colors when light is requested.
    expect(sell.marker.color).not.toBe(getChartColors('dark').markerSell);
  });

  it('lays out customdata as [root, expiration, strike, type, value] per point', () => {
    const markers = [
      makeMarker('sell', {
        tooltip: {
          root: 'R1',
          expiration: '2024-01-01',
          strike: 100,
          type: 'C',
          value: 1.23,
        },
      }),
      makeMarker('sell', {
        tooltip: {
          root: 'R2',
          expiration: '2024-02-01',
          strike: 200,
          type: 'P',
          value: 4.56,
        },
      }),
    ];
    const trace = buildMarkerTrace(markers, 'sell', 'dark');
    expect(trace.customdata).toEqual([
      ['R1', '2024-01-01', 100, 'C', 1.23],
      ['R2', '2024-02-01', 200, 'P', 4.56],
    ]);
  });

  it('uses the helper-built hovertemplate (single source of truth)', () => {
    const trace = buildMarkerTrace([makeMarker('sell')], 'sell', 'dark');
    expect(trace.hovertemplate).toBe(buildMarkerHovertemplate('sell'));
  });
});

describe('buildAllMarkerTraces', () => {
  it('returns [] for empty/missing markers', () => {
    expect(buildAllMarkerTraces([], 'dark')).toEqual([]);
    expect(buildAllMarkerTraces(undefined, 'dark')).toEqual([]);
    expect(buildAllMarkerTraces(null, 'dark')).toEqual([]);
  });

  it('returns one trace per non-empty kind', () => {
    const traces = buildAllMarkerTraces(
      [makeMarker('sell'), makeMarker('buy')],
      'dark',
    );
    expect(traces).toHaveLength(2);
    const names = traces.map((t) => t.name);
    expect(names).toContain('Roll — sell');
    expect(names).toContain('Roll — buy');
  });

  it('omits the kind when only the other side is present', () => {
    const onlySell = buildAllMarkerTraces([makeMarker('sell')], 'dark');
    expect(onlySell).toHaveLength(1);
    expect(onlySell[0].marker.symbol).toBe('circle-open');

    const onlyBuy = buildAllMarkerTraces([makeMarker('buy')], 'dark');
    expect(onlyBuy).toHaveLength(1);
    expect(onlyBuy[0].marker.symbol).toBe('circle');
  });

  it('groups markers of the same kind into a single trace', () => {
    const traces = buildAllMarkerTraces(
      [makeMarker('sell', { x: 'a' }), makeMarker('sell', { x: 'b' })],
      'dark',
    );
    expect(traces).toHaveLength(1);
    expect(traces[0].x).toEqual(['a', 'b']);
  });

  it('ignores markers with unknown kinds (defensive)', () => {
    const traces = buildAllMarkerTraces(
      [makeMarker('sell'), { x: 'x', y: 0, kind: 'mystery', tooltip: {} }],
      'dark',
    );
    expect(traces).toHaveLength(1);
  });

  it('returns the SELL trace LAST so the hollow ring renders on top at overlap (CONTRACT §C)', () => {
    // Plotly draws later traces ON TOP. For the overlap case (sell + buy
    // sharing x,y) the hollow `circle-open` (sell) must render over the
    // filled `circle` (buy). This pins the MARKER_STYLE insertion order
    // (buy first, sell last) that drives the iteration in
    // buildAllMarkerTraces. Any reorder must trip this test.
    const traces = buildAllMarkerTraces(
      [makeMarker('sell'), makeMarker('buy')],
      'dark',
    );
    expect(traces).toHaveLength(2);
    expect(traces[traces.length - 1].marker.symbol).toBe('circle-open');
    expect(traces[traces.length - 1].name).toBe('Roll — sell');
    expect(traces[0].marker.symbol).toBe('circle');
    expect(traces[0].name).toBe('Roll — buy');
  });

  it('order is independent of input array order — driven by MARKER_STYLE only', () => {
    // Input order is buy-then-sell here; output must still be buy, sell.
    const traces = buildAllMarkerTraces(
      [makeMarker('buy'), makeMarker('sell')],
      'dark',
    );
    expect(traces).toHaveLength(2);
    expect(traces[0].marker.symbol).toBe('circle');
    expect(traces[1].marker.symbol).toBe('circle-open');
  });
});

describe('hovertemplate override + caller-controlled customdata (futures-roll-markers)', () => {
  // The chartMarkers API was extended (CONTRACT §B) to support non-options
  // callers — futures has only {contract_id, price}, options has all 5 fields.
  // The extension is purely additive: options callers pass no override and
  // continue to work unchanged (pinned by the existing tests above).

  it('buildMarkerTrace uses the explicit hovertemplate arg instead of the default', () => {
    const customTpl = '<b>Custom</b><br>%{customdata[0]}<extra></extra>';
    const trace = buildMarkerTrace([makeMarker('sell')], 'sell', 'dark', customTpl);
    expect(trace).not.toBeNull();
    expect(trace.hovertemplate).toBe(customTpl);
    // And the default is NOT used.
    expect(trace.hovertemplate).not.toBe(buildMarkerHovertemplate('sell'));
  });

  it('buildAllMarkerTraces opts.hovertemplates.sell overrides sell only — buy unchanged', () => {
    const sellTpl = '<b>Sell</b><br>%{customdata[0]}<extra></extra>';
    const traces = buildAllMarkerTraces(
      [makeMarker('sell'), makeMarker('buy')],
      'dark',
      { hovertemplates: { sell: sellTpl } },
    );
    expect(traces).toHaveLength(2);
    const sell = traces.find((t) => t.name === 'Roll — sell');
    const buy = traces.find((t) => t.name === 'Roll — buy');
    expect(sell.hovertemplate).toBe(sellTpl);
    expect(buy.hovertemplate).toBe(buildMarkerHovertemplate('buy'));
  });

  it('buildAllMarkerTraces opts.hovertemplates.buy overrides buy only — sell unchanged', () => {
    const buyTpl = '<b>Buy</b><br>%{customdata[0]}<extra></extra>';
    const traces = buildAllMarkerTraces(
      [makeMarker('sell'), makeMarker('buy')],
      'dark',
      { hovertemplates: { buy: buyTpl } },
    );
    expect(traces).toHaveLength(2);
    const sell = traces.find((t) => t.name === 'Roll — sell');
    const buy = traces.find((t) => t.name === 'Roll — buy');
    expect(buy.hovertemplate).toBe(buyTpl);
    expect(sell.hovertemplate).toBe(buildMarkerHovertemplate('sell'));
  });

  it('Marker with explicit customdata array uses it VERBATIM (skips tooltip derivation)', () => {
    // Futures-style marker: 2-element customdata [contract_id, price]. The
    // tooltip-derivation path would produce 5 elements — caller-supplied
    // customdata must win regardless.
    const marker = {
      x: '2024-03-15',
      y: 100.5,
      kind: 'sell',
      customdata: ['FUT_OLD', 100.5],
      // Tooltip also present — must be IGNORED when customdata is supplied.
      tooltip: { root: 'IGNORED', expiration: 'IGNORED', strike: 999, type: 'X', value: 999 },
    };
    const trace = buildMarkerTrace([marker], 'sell', 'dark');
    expect(trace.customdata).toEqual([['FUT_OLD', 100.5]]);
  });

  it('Marker with no customdata and no tooltip does not crash — produces [undefined×5]', () => {
    // Regression guard: a malformed marker (neither tooltip nor customdata)
    // must NOT throw — it falls back to the options-shape derivation with
    // every field undefined. The trace is still emitted; Plotly will just
    // render "undefined" in the tooltip slots, which is loud enough.
    const marker = { x: '2024-03-15', y: 100.5, kind: 'sell' };
    expect(() => buildMarkerTrace([marker], 'sell', 'dark')).not.toThrow();
    const trace = buildMarkerTrace([marker], 'sell', 'dark');
    expect(trace.customdata).toEqual([[undefined, undefined, undefined, undefined, undefined]]);
  });
});

describe('MARKER_STYLE modularity (verb in map, Object.keys iteration)', () => {
  it('hovertemplate verb is read from MARKER_STYLE — no inline kind branch', () => {
    // Sanity: distinct kinds produce distinct verbs that originate from the
    // map. (This is structurally guaranteed by the implementation; the test
    // pins the contract so a regression to inline branching is loud.)
    expect(buildMarkerHovertemplate('sell')).toContain('<b>Sell</b>');
    expect(buildMarkerHovertemplate('buy')).toContain('<b>Buy</b>');
    // Unknown kind → empty string (NOT a fallback verb).
    expect(buildMarkerHovertemplate('mystery')).toBe('');
  });
});
