// Data-driven tests for the default indicator registry.
//
// Every entry in DEFAULT_INDICATORS is validated against a locally hardcoded
// expectation table (param names/types/defaults and series labels). If an
// indicator drifts from the expectation, the test fails loudly with the id in
// the message — we never silently patch defaults to match the test.
//
// Also enforces shape invariants (readonly/params/seriesMap/code),
// library-wide invariants (count, unique ids, kebab-case), and that the
// derived spec from parseIndicatorSpec matches what the UI will show.
//
// Library shape is the post-2026-04-pruning set: 10 canonical JS entries
// (sma, ema, rsi, macd triple, bollinger quad) plus 13 legacy-port entries
// translated from the Java simulator. See ``docs/indicators.md``.

import { describe, it, expect } from 'vitest';
import { DEFAULT_INDICATORS } from './defaultIndicators';
import { parseIndicatorSpec } from './paramParser';

// ---------------------------------------------------------------------------
// Expectation table — mirrors the brief. Single source of truth for the test.
// ---------------------------------------------------------------------------
const EXPECTATIONS = {
  // --- Canonical ---------------------------------------------------------
  sma:                    { params: [{ name: 'window', type: 'int', default: 20 }],                                                                                                                                seriesLabels: ['close'],                  ownPanel: false },
  ema:                    { params: [{ name: 'window', type: 'int', default: 20 }],                                                                                                                                seriesLabels: ['close'],                  ownPanel: false },
  rsi:                    { params: [{ name: 'window', type: 'int', default: 14 }],                                                                                                                                seriesLabels: ['close'],                  ownPanel: true  },
  'macd-line':            { params: [{ name: 'fast', type: 'int', default: 12 }, { name: 'slow', type: 'int', default: 26 }],                                                                                      seriesLabels: ['close'],                  ownPanel: true  },
  'macd-signal':          { params: [{ name: 'fast', type: 'int', default: 12 }, { name: 'slow', type: 'int', default: 26 }, { name: 'signal', type: 'int', default: 9 }],                                         seriesLabels: ['close'],                  ownPanel: true  },
  'macd-histogram':       { params: [{ name: 'fast', type: 'int', default: 12 }, { name: 'slow', type: 'int', default: 26 }, { name: 'signal', type: 'int', default: 9 }],                                         seriesLabels: ['close'],                  ownPanel: true  },
  'bollinger-upper':      { params: [{ name: 'window', type: 'int', default: 20 }, { name: 'num_std', type: 'float', default: 2.0 }],                                                                              seriesLabels: ['close'],                  ownPanel: false },
  'bollinger-middle':     { params: [{ name: 'window', type: 'int', default: 20 }],                                                                                                                                seriesLabels: ['close'],                  ownPanel: false },
  'bollinger-lower':      { params: [{ name: 'window', type: 'int', default: 20 }, { name: 'num_std', type: 'float', default: 2.0 }],                                                                              seriesLabels: ['close'],                  ownPanel: false },
  'bollinger-percent-b':  { params: [{ name: 'window', type: 'int', default: 20 }, { name: 'num_std', type: 'float', default: 2.0 }],                                                                              seriesLabels: ['close'],                  ownPanel: true  },

  // --- Legacy Java ports ------------------------------------------------
  atr:                           { params: [{ name: 'window', type: 'int', default: 14 }],                                                                                                                          seriesLabels: ['high', 'low', 'close'],  ownPanel: true  },
  'absolute-mean':               { params: [{ name: 'window', type: 'int', default: 20 }],                                                                                                                          seriesLabels: ['close'],                 ownPanel: false },
  impetus:                       { params: [{ name: 'window', type: 'int', default: 14 }],                                                                                                                          seriesLabels: ['close'],                 ownPanel: true  },
  'weighted-impetus':            { params: [{ name: 'window', type: 'int', default: 14 }],                                                                                                                          seriesLabels: ['close'],                 ownPanel: true  },
  'centred-slope':               { params: [{ name: 'window', type: 'int', default: 1 }],                                                                                                                           seriesLabels: ['close'],                 ownPanel: true  },
  'slope-acceleration':          { params: [],                                                                                                                                                                      seriesLabels: ['close'],                 ownPanel: true  },
  'slope-statistics':            { params: [{ name: 'window', type: 'int', default: 20 }],                                                                                                                          seriesLabels: ['close'],                 ownPanel: true  },
  'rolling-percentile-bands':    { params: [{ name: 'window', type: 'int', default: 252 }, { name: 'percentile', type: 'float', default: 95.0 }],                                                                    seriesLabels: ['close'],                 ownPanel: false },
  'percentile-filtered-return':  { params: [{ name: 'window', type: 'int', default: 252 }, { name: 'filter_window', type: 'int', default: 50 }, { name: 'percentile', type: 'float', default: 95.0 }],              seriesLabels: ['close'],                 ownPanel: true  },
  'trailing-extreme':            { params: [{ name: 'window', type: 'int', default: 20 }, { name: 'use_min', type: 'bool', default: false }],                                                                        seriesLabels: ['close'],                 ownPanel: false },
  'engulfment-pattern':          { params: [{ name: 'min_engulfing_periods', type: 'int', default: 5 }],                                                                                                             seriesLabels: ['open', 'high', 'low'],   ownPanel: false },
  'engulfment-exit':             { params: [{ name: 'box_lookback', type: 'int', default: 20 }, { name: 'ratio_win', type: 'float', default: 2.0 }, { name: 'ratio_loss', type: 'float', default: 1.0 }],            seriesLabels: ['open', 'high', 'low', 'entry'], ownPanel: false },
  'swing-pivots':                { params: [{ name: 'total_periods', type: 'int', default: 20 }, { name: 'inflection_periods', type: 'int', default: 5 }],                                                           seriesLabels: ['close'],                 ownPanel: false },
};

const KEBAB_CASE_RE = /^[a-z][a-z0-9-]*$/;

// Canonical category set. Must stay in sync with the ``category`` field
// documented in ``defaultIndicators.js`` and set on every entry file under
// ``defaults/*.js``.
const CATEGORIES = ['trend', 'momentum', 'volatility', 'pattern', 'statistical'];

describe('DEFAULT_INDICATORS — library invariants', () => {
  it('contains exactly 23 entries', () => {
    expect(DEFAULT_INDICATORS).toHaveLength(23);
  });

  it('has unique ids', () => {
    const ids = DEFAULT_INDICATORS.map((e) => e.id);
    const unique = new Set(ids);
    expect(unique.size).toBe(ids.length);
  });

  it('has kebab-case ids', () => {
    for (const entry of DEFAULT_INDICATORS) {
      expect(
        KEBAB_CASE_RE.test(entry.id),
        `id ${JSON.stringify(entry.id)} does not match /^[a-z][a-z0-9-]*$/`,
      ).toBe(true);
    }
  });

  it('has an expectation row for every entry (and only for entries that exist)', () => {
    const ids = DEFAULT_INDICATORS.map((e) => e.id).sort();
    const expected = Object.keys(EXPECTATIONS).sort();
    expect(ids).toEqual(expected);
  });

  it('splits overlay / own-panel across defaults as expected', () => {
    const overlay = DEFAULT_INDICATORS.filter((e) => e.ownPanel === false).length;
    const ownPanel = DEFAULT_INDICATORS.filter((e) => e.ownPanel === true).length;
    const expectedOverlay = Object.values(EXPECTATIONS).filter((e) => e.ownPanel === false).length;
    const expectedOwnPanel = Object.values(EXPECTATIONS).filter((e) => e.ownPanel === true).length;
    expect(overlay).toBe(expectedOverlay);
    expect(ownPanel).toBe(expectedOwnPanel);
    // Sanity: the two counts must sum to the registry length.
    expect(overlay + ownPanel).toBe(DEFAULT_INDICATORS.length);
  });
});

describe('DEFAULT_INDICATORS — per-entry shape and spec', () => {
  for (const entry of DEFAULT_INDICATORS) {
    describe(`${entry.id}`, () => {
      it('has the canonical entry shape', () => {
        expect(typeof entry.id).toBe('string');
        expect(entry.id.length).toBeGreaterThan(0);
        expect(typeof entry.name).toBe('string');
        expect(entry.name.length).toBeGreaterThan(0);
        expect(entry.readonly).toBe(true);
        expect(typeof entry.code).toBe('string');
        expect(entry.code.length).toBeGreaterThan(0);
        expect(entry.params).toEqual({});
        expect(entry.seriesMap).toEqual({});
        expect(typeof entry.doc).toBe('string');
        // Extensive-doc quality bar: every shipped entry must carry
        // intuition + formula + parameters + edge-cases sections. The
        // threshold is conservative (300 chars) — current docs are all
        // well above that and the assertion fails loudly if any entry
        // regresses to a one-line stub.
        expect(
          entry.doc.length,
          `doc for id ${JSON.stringify(entry.id)} is too thin (< 300 chars)`,
        ).toBeGreaterThan(300);
        // Every doc must contain an Intuition section, a Formula section,
        // and an Edge cases section. Parameters section is also required
        // even for parameterless entries, to state "None" explicitly.
        for (const marker of ['**Intuition', '**Formula', '**Parameters', '**Edge cases']) {
          expect(
            entry.doc.includes(marker),
            `doc for id ${JSON.stringify(entry.id)} is missing section ${JSON.stringify(marker)}`,
          ).toBe(true);
        }
        expect(
          typeof entry.ownPanel,
          `ownPanel must be boolean for id ${JSON.stringify(entry.id)}`,
        ).toBe('boolean');
        // Category field — the single source of truth for library grouping.
        expect(
          typeof entry.category,
          `category must be a string for id ${JSON.stringify(entry.id)}`,
        ).toBe('string');
        expect(
          CATEGORIES,
          `category ${JSON.stringify(entry.category)} for id ${JSON.stringify(entry.id)} is not one of the canonical buckets`,
        ).toContain(entry.category);
      });

      it('ships the expected ownPanel flag', () => {
        const expected = EXPECTATIONS[entry.id];
        expect(
          entry.ownPanel,
          `ownPanel mismatch for id ${JSON.stringify(entry.id)}`,
        ).toBe(expected.ownPanel);
      });

      it('chartMode, if set, is a supported Plotly mode', () => {
        // chartMode is optional — when absent the chart defaults to 'lines'.
        // When present it must be one of the supported Plotly scatter modes;
        // the chart passes it straight through to trace.mode.
        if (entry.chartMode === undefined) return;
        expect(
          ['lines', 'markers', 'lines+markers'],
          `chartMode for id ${JSON.stringify(entry.id)} must be lines|markers|lines+markers`,
        ).toContain(entry.chartMode);
      });

      it('parses to the expected params and series labels', () => {
        const expected = EXPECTATIONS[entry.id];
        expect(
          expected,
          `no expectation row for id ${JSON.stringify(entry.id)}`,
        ).toBeDefined();

        const spec = parseIndicatorSpec(entry.code);

        // Compare series labels first — mismatches here usually signal a
        // body-level drift rather than a signature one. Expectation and
        // observed are compared as sets (the order of labels in the
        // source body is not a contract — only the label set is).
        expect(
          new Set(spec.seriesLabels),
          `seriesLabels mismatch for id ${JSON.stringify(entry.id)}`,
        ).toEqual(new Set(expected.seriesLabels));

        // Compare params including typed defaults. toEqual does a structural
        // compare so int 20 vs float 20.0 would still match numerically; we
        // additionally assert the JS types line up with the annotation.
        expect(
          spec.params,
          `params mismatch for id ${JSON.stringify(entry.id)}`,
        ).toEqual(expected.params);

        for (let i = 0; i < expected.params.length; i += 1) {
          const p = spec.params[i];
          const e = expected.params[i];
          if (e.type === 'bool') {
            expect(typeof p.default).toBe('boolean');
          } else {
            expect(typeof p.default).toBe('number');
            expect(Number.isFinite(p.default)).toBe(true);
          }
        }
      });
    });
  }
});

describe('DEFAULT_INDICATORS — category coverage (post 2026-04 rework)', () => {
  // The post-rework library covers five grouping buckets. Membership is
  // derived directly from each entry's ``category`` field — that field is
  // the single source of truth. The exact registry order is not asserted
  // (that's plumbing, not a contract), but the presence of at least one
  // representative per bucket IS part of the product contract — we don't
  // want a future refactor to silently drop an entire category.
  const BUCKET_MEMBERS = CATEGORIES.reduce((acc, cat) => {
    acc[cat] = new Set(
      DEFAULT_INDICATORS.filter((e) => e.category === cat).map((e) => e.id),
    );
    return acc;
  }, {});

  it('registry ids partition exactly into the five buckets', () => {
    const allBucketIds = new Set();
    for (const members of Object.values(BUCKET_MEMBERS)) {
      for (const id of members) allBucketIds.add(id);
    }
    const registryIds = new Set(DEFAULT_INDICATORS.map((e) => e.id));
    expect(registryIds).toEqual(allBucketIds);
  });

  for (const bucket of CATEGORIES) {
    it(`${bucket} bucket has at least one entry`, () => {
      expect(
        BUCKET_MEMBERS[bucket].size,
        `${bucket} bucket has no representatives in DEFAULT_INDICATORS`,
      ).toBeGreaterThan(0);
    });
  }
});
