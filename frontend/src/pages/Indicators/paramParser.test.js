import { describe, it, expect, vi, afterEach } from 'vitest';
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from './paramParser';

// ---------------------------------------------------------------------------
// parseIndicatorSpec — typed parameters
// ---------------------------------------------------------------------------
describe('parseIndicatorSpec — params', () => {
  it('returns empty spec for empty / whitespace / nullish input', () => {
    expect(parseIndicatorSpec('')).toEqual({ params: [], seriesLabels: [] });
    expect(parseIndicatorSpec('   \n\t  ')).toEqual({ params: [], seriesLabels: [] });
    expect(parseIndicatorSpec(null)).toEqual({ params: [], seriesLabels: [] });
    expect(parseIndicatorSpec(undefined)).toEqual({ params: [], seriesLabels: [] });
  });

  it('extracts a typed int param', () => {
    const code = `def compute(series, window: int = 20):\n    return series['price']`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'window', type: 'int', default: 20 },
    ]);
  });

  it('extracts a typed float param (positive + negative)', () => {
    const code = `def compute(series, threshold: float = 0.5, offset: float = -1.25):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'threshold', type: 'float', default: 0.5 },
      { name: 'offset', type: 'float', default: -1.25 },
    ]);
  });

  it('extracts a typed bool param (True and False defaults)', () => {
    const code = `def compute(series, use_log: bool = False, smooth: bool = True):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'use_log', type: 'bool', default: false },
      { name: 'smooth', type: 'bool', default: true },
    ]);
  });

  it('accepts scientific notation float defaults', () => {
    const code = `def compute(series, a: float = 1e3, b: float = -2.5e-4, c: float = 1.5e+10):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'a', type: 'float', default: 1000 },
      { name: 'b', type: 'float', default: -0.00025 },
      { name: 'c', type: 'float', default: 15000000000 },
    ]);
  });

  it('accepts scientific notation int default when integer-valued', () => {
    // 1e3 is 1000 — valid integer value
    const code = `def compute(series, n: int = 1e3):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'n', type: 'int', default: 1000 },
    ]);
  });

  it('rejects non-integer-valued default for int annotation', () => {
    const code = `def compute(series, bad: int = 1.5):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([]);
  });

  it('skips unannotated params silently', () => {
    const code = `def compute(series, window = 20, threshold: float = 0.5):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'threshold', type: 'float', default: 0.5 },
    ]);
  });

  it('skips params with non-whitelisted annotations (str, list, typed generics)', () => {
    const code = `def compute(series, label: str = "x", items: list = None, w: int = 20):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'w', type: 'int', default: 20 },
    ]);
  });

  it('skips params with no default', () => {
    const code = `def compute(series, window: int, threshold: float = 0.5):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'threshold', type: 'float', default: 0.5 },
    ]);
  });

  it('skips params whose default is not a literal', () => {
    const code = `def compute(series, w: int = abs(-1), t: float = 0.5):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 't', type: 'float', default: 0.5 },
    ]);
  });

  it('rejects True/False as int default', () => {
    const code = `def compute(series, w: int = True):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([]);
  });

  it('rejects numeric literal as bool default', () => {
    const code = `def compute(series, b: bool = 0):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([]);
  });

  it('skips leading *args / **kwargs markers', () => {
    const code = `def compute(series, *args, w: int = 10, **kwargs):
    pass`;
    expect(parseIndicatorSpec(code).params).toEqual([
      { name: 'w', type: 'int', default: 10 },
    ]);
  });

  it('handles multi-line def signature', () => {
    const code = `def compute(
    series,
    window: int = 20,
    threshold: float = 0.5,
    use_log: bool = False,
):
    return series['price']`;
    const spec = parseIndicatorSpec(code);
    expect(spec.params).toEqual([
      { name: 'window', type: 'int', default: 20 },
      { name: 'threshold', type: 'float', default: 0.5 },
      { name: 'use_log', type: 'bool', default: false },
    ]);
  });

  it('returns empty params when no def compute signature present', () => {
    expect(parseIndicatorSpec('x = 1').params).toEqual([]);
  });

  it('produces empty spec without throwing on malformed signature', () => {
    // Unbalanced parens
    expect(() => parseIndicatorSpec('def compute(series, w: int = 20')).not.toThrow();
    expect(parseIndicatorSpec('def compute(series, w: int = 20').params).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// parseIndicatorSpec — series label extraction
// ---------------------------------------------------------------------------
describe('parseIndicatorSpec — seriesLabels', () => {
  it('returns [] when body has no series access', () => {
    const code = `def compute(series, w: int = 20):
    return 42`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual([]);
  });

  it('extracts a single series label', () => {
    const code = `def compute(series):
    return series['price']`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['price']);
  });

  it('extracts multiple labels with mixed quoting', () => {
    const code = `def compute(series):
    p = series['price']
    v = series["vix"]
    r = series['rate']
    return p - v + r`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['price', 'vix', 'rate']);
  });

  it('dedupes while preserving order', () => {
    const code = `def compute(series):
    a = series['price']
    b = series['vix']
    c = series["price"]
    return a + b + c`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['price', 'vix']);
  });

  it('ignores series accesses inside string literals', () => {
    const code = `def compute(series):
    doc = "series['fake']"
    x = series['real']
    return x`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['real']);
  });

  it('ignores series accesses inside triple-quoted docstrings', () => {
    const code = `def compute(series):
    """series['ghost'] docs"""
    return series['seen']`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['seen']);
  });

  it('ignores series accesses inside line comments', () => {
    const code = `def compute(series):
    # series['hidden']
    return series['shown']`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['shown']);
  });

  it('skips non-identifier labels (whitespace, hyphens) silently', () => {
    const code = `def compute(series):
    a = series['foo-bar']
    b = series['ok']
    return b`;
    expect(parseIndicatorSpec(code).seriesLabels).toEqual(['ok']);
  });
});

// ---------------------------------------------------------------------------
// reconcileParams
// ---------------------------------------------------------------------------
describe('reconcileParams', () => {
  it('keeps existing numeric value for int param still present', () => {
    const existing = { window: 50 };
    const parsed = [{ name: 'window', type: 'int', default: 20 }];
    expect(reconcileParams(existing, parsed)).toEqual({ window: 50 });
  });

  it('keeps existing boolean value for bool param still present', () => {
    const existing = { use_log: true };
    const parsed = [{ name: 'use_log', type: 'bool', default: false }];
    expect(reconcileParams(existing, parsed)).toEqual({ use_log: true });
  });

  it('uses parsed default for brand-new params', () => {
    const parsed = [
      { name: 'window', type: 'int', default: 20 },
      { name: 'use_log', type: 'bool', default: false },
    ];
    expect(reconcileParams({ window: 50 }, parsed)).toEqual({ window: 50, use_log: false });
  });

  it('drops params no longer in parsed spec', () => {
    const existing = { window: 50, dead: 99 };
    const parsed = [{ name: 'window', type: 'int', default: 20 }];
    expect(reconcileParams(existing, parsed)).toEqual({ window: 50 });
  });

  it('replaces wrong-type existing value with parsed default (bool)', () => {
    const existing = { use_log: 'nope' };
    const parsed = [{ name: 'use_log', type: 'bool', default: true }];
    expect(reconcileParams(existing, parsed)).toEqual({ use_log: true });
  });

  it('replaces wrong-type existing value with parsed default (int/float)', () => {
    const existing = { window: 'nope', alpha: NaN };
    const parsed = [
      { name: 'window', type: 'int', default: 20 },
      { name: 'alpha', type: 'float', default: 0.5 },
    ];
    expect(reconcileParams(existing, parsed)).toEqual({ window: 20, alpha: 0.5 });
  });

  it('handles empty inputs', () => {
    expect(reconcileParams({}, [])).toEqual({});
    expect(reconcileParams(null, [])).toEqual({});
    expect(reconcileParams(undefined, [{ name: 'a', type: 'int', default: 7 }])).toEqual({ a: 7 });
  });
});

// ---------------------------------------------------------------------------
// reconcileSeriesMap
// ---------------------------------------------------------------------------
describe('reconcileSeriesMap', () => {
  it('preserves picks for labels still present', () => {
    const existing = { price: { collection: 'INDEX', instrument_id: '^GSPC' } };
    expect(reconcileSeriesMap(existing, ['price'])).toEqual({
      price: { collection: 'INDEX', instrument_id: '^GSPC' },
    });
  });

  it('drops labels no longer referenced', () => {
    const existing = {
      price: { collection: 'INDEX', instrument_id: '^GSPC' },
      dead: { collection: 'X', instrument_id: 'Y' },
    };
    expect(reconcileSeriesMap(existing, ['price'])).toEqual({
      price: { collection: 'INDEX', instrument_id: '^GSPC' },
    });
  });

  it('adds brand-new labels as null slots', () => {
    expect(reconcileSeriesMap({}, ['price', 'vix'])).toEqual({
      price: null,
      vix: null,
    });
  });

  it('resets malformed existing entries to null', () => {
    const existing = { price: { collection: 'INDEX' /* no instrument_id */ } };
    expect(reconcileSeriesMap(existing, ['price'])).toEqual({ price: null });
  });
});

// ---------------------------------------------------------------------------
// parseIndicatorSpec — author-feedback warnings for silent-skips
// ---------------------------------------------------------------------------
describe('parseIndicatorSpec — silent-skip warnings', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('warns once when a param is skipped because its annotation is unsupported', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const code = `def compute(series, label: str = "x"):
    pass`;
    const spec = parseIndicatorSpec(code);
    expect(spec.params).toEqual([]);
    expect(warn).toHaveBeenCalledTimes(1);
    const msg = warn.mock.calls[0][0];
    expect(msg).toContain('label');
    expect(msg).toContain('str');
  });

  it('warns once when a param is skipped because its default is not a literal', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const code = `def compute(series, w: int = abs(-1)):
    pass`;
    const spec = parseIndicatorSpec(code);
    expect(spec.params).toEqual([]);
    expect(warn).toHaveBeenCalledTimes(1);
    const msg = warn.mock.calls[0][0];
    expect(msg).toContain('w');
  });
});
