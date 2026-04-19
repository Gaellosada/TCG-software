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

import { describe, it, expect } from 'vitest';
import { DEFAULT_INDICATORS } from './defaultIndicators';
import { parseIndicatorSpec } from './paramParser';

// ---------------------------------------------------------------------------
// Expectation table — mirrors the brief. Single source of truth for the test.
// ---------------------------------------------------------------------------
const EXPECTATIONS = {
  sma:                 { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  ema:                 { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  wma:                 { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  dema:                { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  tema:                { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  kama:                { params: [{ name: 'window', type: 'int',   default: 10  }, { name: 'fast',   type: 'int',   default: 2   }, { name: 'slow', type: 'int', default: 30 }], seriesLabels: ['close'], ownPanel: false },
  rsi:                 { params: [{ name: 'window', type: 'int',   default: 14  }],                                                                       seriesLabels: ['close'], ownPanel: true },
  roc:                 { params: [{ name: 'window', type: 'int',   default: 10  }],                                                                       seriesLabels: ['close'], ownPanel: true },
  momentum:            { params: [{ name: 'window', type: 'int',   default: 10  }],                                                                       seriesLabels: ['close'], ownPanel: true },
  'macd-line':         { params: [{ name: 'fast',   type: 'int',   default: 12  }, { name: 'slow',   type: 'int',   default: 26  }],                       seriesLabels: ['close'], ownPanel: true },
  'macd-signal':       { params: [{ name: 'fast',   type: 'int',   default: 12  }, { name: 'slow',   type: 'int',   default: 26  }, { name: 'signal', type: 'int', default: 9 }], seriesLabels: ['close'], ownPanel: true },
  'macd-histogram':    { params: [{ name: 'fast',   type: 'int',   default: 12  }, { name: 'slow',   type: 'int',   default: 26  }, { name: 'signal', type: 'int', default: 9 }], seriesLabels: ['close'], ownPanel: true },
  'bollinger-upper':   { params: [{ name: 'window', type: 'int',   default: 20  }, { name: 'num_std', type: 'float', default: 2.0 }],                      seriesLabels: ['close'], ownPanel: false },
  'bollinger-middle':  { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  'bollinger-lower':   { params: [{ name: 'window', type: 'int',   default: 20  }, { name: 'num_std', type: 'float', default: 2.0 }],                      seriesLabels: ['close'], ownPanel: false },
  'bollinger-percent-b': { params: [{ name: 'window', type: 'int', default: 20  }, { name: 'num_std', type: 'float', default: 2.0 }],                     seriesLabels: ['close'], ownPanel: true },
  'rolling-stddev':    { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: true },
  'log-return':        { params: [{ name: 'window', type: 'int',   default: 1   }],                                                                       seriesLabels: ['close'], ownPanel: true },
  'simple-return':     { params: [{ name: 'window', type: 'int',   default: 1   }],                                                                       seriesLabels: ['close'], ownPanel: true },
  'rolling-zscore':    { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: true },
  'rolling-min':       { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
  'rolling-max':       { params: [{ name: 'window', type: 'int',   default: 20  }],                                                                       seriesLabels: ['close'], ownPanel: false },
};

const KEBAB_CASE_RE = /^[a-z][a-z0-9-]*$/;

describe('DEFAULT_INDICATORS — library invariants', () => {
  it('contains exactly 22 entries', () => {
    expect(DEFAULT_INDICATORS).toHaveLength(22);
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

  it('splits 11 overlay / 11 own-panel across defaults', () => {
    const overlay = DEFAULT_INDICATORS.filter((e) => e.ownPanel === false).length;
    const ownPanel = DEFAULT_INDICATORS.filter((e) => e.ownPanel === true).length;
    expect(overlay).toBe(11);
    expect(ownPanel).toBe(11);
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
        expect(entry.doc.length).toBeGreaterThan(20);
        expect(
          typeof entry.ownPanel,
          `ownPanel must be boolean for id ${JSON.stringify(entry.id)}`,
        ).toBe('boolean');
      });

      it('ships the expected ownPanel flag', () => {
        const expected = EXPECTATIONS[entry.id];
        expect(
          entry.ownPanel,
          `ownPanel mismatch for id ${JSON.stringify(entry.id)}`,
        ).toBe(expected.ownPanel);
      });

      it('parses to the expected params and series labels', () => {
        const expected = EXPECTATIONS[entry.id];
        expect(
          expected,
          `no expectation row for id ${JSON.stringify(entry.id)}`,
        ).toBeDefined();

        const spec = parseIndicatorSpec(entry.code);

        // Compare series labels first — mismatches here usually signal a
        // body-level drift rather than a signature one.
        expect(
          spec.seriesLabels,
          `seriesLabels mismatch for id ${JSON.stringify(entry.id)}`,
        ).toEqual(expected.seriesLabels);

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
