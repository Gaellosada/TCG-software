// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import OptionStreamForm, {
  buildDefaultOptionStream,
  validateOptionStream,
  MID_TOOLTIP,
  SYNTHETIC_WEEKLY_LABEL,
  CYCLE_LABELS,
  deriveCycleOptions,
  pickDefaultCycle,
  SIZING_MODE_LABELS,
  SIZE_LABEL_FUTURES,
  SIZE_LABEL_PREMIUM,
} from './OptionStreamForm';

afterEach(cleanup);

const ROOTS = [
  {
    collection: 'OPT_SP_500',
    root_label: 'SP 500',
    has_greeks: true,
  },
  {
    collection: 'OPT_VIX',
    root_label: 'VIX',
    has_greeks: false,
  },
];

function renderForm(overrides = {}) {
  const onChange = overrides.onChange || vi.fn();
  const value = overrides.value !== undefined
    ? overrides.value
    : buildDefaultOptionStream({ availableRoots: ROOTS });
  const props = {
    value,
    onChange,
    availableRoots: ROOTS,
    ...overrides,
  };
  return { onChange, value, ...render(<OptionStreamForm {...props} />) };
}

describe('<OptionStreamForm>', () => {
  it('renders all field selectors with default allowed sets', () => {
    renderForm();
    expect(screen.getByLabelText('Root')).toBeTruthy();
    expect(screen.getByLabelText('Cycle')).toBeTruthy();
    expect(screen.getByLabelText('Maturity rule')).toBeTruthy();
    expect(screen.getByLabelText('Selection criterion')).toBeTruthy();
    expect(screen.getByLabelText('Series')).toBeTruthy();
    // type radios
    expect(screen.getByRole('radio', { name: 'Call' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'Put' })).toBeTruthy();
  });

  // The Series picker is a plainly-labelled control (no longer hidden behind
  // an "Advanced" disclosure), defaulting to `mid`. The legacy disclosure
  // wrapper is gone entirely.
  it('presents Series as a plainly-labelled control defaulting to mid', () => {
    renderForm();
    // No <details> disclosure remains.
    expect(screen.queryByTestId('stream-advanced')).toBeNull();
    const seriesSelect = screen.getByLabelText('Series');
    expect(seriesSelect).toBeTruthy();
    expect(seriesSelect.tagName.toLowerCase()).toBe('select');
    expect(seriesSelect.value).toBe('mid');
  });

  it('labels mid as "Mid price" and iv as "Implied volatility" in the Series options', () => {
    renderForm();
    const seriesSelect = screen.getByLabelText('Series');
    const labels = Array.from(seriesSelect.querySelectorAll('option')).map((o) => o.textContent);
    expect(labels).toContain('Mid price');
    expect(labels).toContain('Implied volatility');
    expect(labels).toContain('Open interest');
  });

  it('offers bs_mid ("BS mid (from IV)") in the Series options and selects it', () => {
    const onChange = vi.fn();
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    renderForm({ value, onChange });
    const seriesSelect = screen.getByLabelText('Series');
    // The BS-from-IV option is present with its clear label.
    const options = Array.from(seriesSelect.querySelectorAll('option'));
    const bs = options.find((o) => o.value === 'bs_mid');
    expect(bs).toBeTruthy();
    expect(bs.textContent).toBe('BS mid (from IV)');
    // Selecting it emits stream: 'bs_mid'.
    fireEvent.change(seriesSelect, { target: { value: 'bs_mid' } });
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.stream).toBe('bs_mid');
  });

  // Issue #2 (D1): in the PORTFOLIO add-holding flow an option leg is just the
  // option's PRICE (mid). The stream concept (iv/delta/greeks/volume) is a
  // SIGNAL-level operand, not a portfolio concern — so when restricted to a
  // single stream the form must NOT render a (pointless 1-item) Series
  // selector at all.
  describe('single-stream restriction (portfolio price-only)', () => {
    it('renders NO Series selector when allowedStreams is just [mid]', () => {
      renderForm({ allowedStreams: ['mid'] });
      // No Series <select> / control, and no advanced disclosure.
      expect(screen.queryByLabelText('Series')).toBeNull();
      expect(screen.queryByTestId('stream-advanced')).toBeNull();
      // The rest of the form is intact.
      expect(screen.getByLabelText('Root')).toBeTruthy();
      expect(screen.getByLabelText('Maturity rule')).toBeTruthy();
      expect(screen.getByLabelText('Selection criterion')).toBeTruthy();
    });

    it('forces the value stream to the single allowed stream', () => {
      // Parent passes a stale value with stream='iv'; the restricted form must
      // coerce it back to the only allowed stream (mid) via onChange.
      const onChange = vi.fn();
      const stale = { ...buildDefaultOptionStream({ availableRoots: ROOTS }), stream: 'iv' };
      renderForm({ value: stale, allowedStreams: ['mid'], onChange });
      expect(onChange).toHaveBeenCalledWith(
        expect.objectContaining({ stream: 'mid' }),
      );
    });

    it('still renders the Series selector for the default (all streams)', () => {
      renderForm();
      expect(screen.getByLabelText('Series')).toBeTruthy();
    });
  });

  it('exposes the exact Mid bid-ask tooltip on the Series control', () => {
    renderForm();
    const tip = screen.getByTestId('mid-tooltip');
    const expected = 'Mid = (bid + ask) / 2 — the quote midpoint, NOT the daily high/low or last/close.';
    expect(tip.getAttribute('title')).toBe(expected);
    expect(tip.getAttribute('aria-label')).toBe(expected);
    // Exported constant must match the locked copy exactly.
    expect(MID_TOOLTIP).toBe(expected);
  });

  it('emits onChange with the correctly-shaped object on root change', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Root'), { target: { value: 'OPT_VIX' } });
    expect(onChange).toHaveBeenCalledOnce();
    const next = onChange.mock.calls[0][0];
    expect(next.type).toBe('option_stream');
    expect(next.collection).toBe('OPT_VIX');
    expect(next.option_type).toBe('C');
    expect(next.maturity.kind).toBe('next_third_friday');
    expect(next.selection.kind).toBe('by_moneyness');
    expect(next.stream).toBe('mid');
  });

  it('emits onChange with full object shape on series change', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Series'), { target: { value: 'iv' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0]).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      stream: 'iv',
    });
  });

  it('switches selection kind to by_moneyness with target/tolerance defaults', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Selection criterion'), {
      target: { value: 'by_moneyness' },
    });
    expect(onChange.mock.calls[0][0].selection).toEqual({
      kind: 'by_moneyness',
      target: 1.0,
      tolerance: 0.05,
    });
  });

  it('switches selection kind to by_delta with signed target for puts', () => {
    const baseValue = buildDefaultOptionStream({ availableRoots: ROOTS });
    baseValue.option_type = 'P';
    const { onChange } = renderForm({ value: baseValue });
    fireEvent.change(screen.getByLabelText('Selection criterion'), {
      target: { value: 'by_delta' },
    });
    expect(onChange.mock.calls[0][0].selection.kind).toBe('by_delta');
    expect(onChange.mock.calls[0][0].selection.target).toBeLessThan(0);
  });

  it('switches maturity kind and emits the new shape', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Maturity rule'), {
      target: { value: 'plus_n_days' },
    });
    expect(onChange.mock.calls[0][0].maturity).toEqual({ kind: 'plus_n_days', n: 30 });
  });

  // ── Unified roll offset {value, unit} (replaces days-only + roll_schedule) ──

  it('renders the Roll offset value+unit defaulting to {0, days}', () => {
    renderForm();
    const value = screen.getByLabelText('Roll offset value');
    const unit = screen.getByLabelText('Roll offset unit');
    expect(value.value).toBe('0');
    expect(unit.value).toBe('days');
  });

  it('does NOT render a Roll schedule control (removed — EOM is the maturity)', () => {
    renderForm();
    expect(screen.queryByLabelText('Roll schedule')).toBeNull();
  });

  it('emits roll_offset {value, unit:days} on value change, clamped 0..365', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '5' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0]).toMatchObject({
      roll_offset: { value: 5, unit: 'days' },
    });
    onChange.mockClear();
    // 90 days (~3 months out) is now valid — the cap was raised from 30 to 365.
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '90' } });
    expect(onChange.mock.calls[0][0].roll_offset).toEqual({ value: 90, unit: 'days' });
    onChange.mockClear();
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '999' } });
    expect(onChange.mock.calls[0][0].roll_offset).toEqual({ value: 365, unit: 'days' });
  });

  it('switches unit to months and re-clamps the value to 0..12', () => {
    // Start from a days value of 20, switch to months → re-clamp to 12.
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.roll_offset = { value: 20, unit: 'days' };
    const { onChange } = renderForm({ value });
    fireEvent.change(screen.getByLabelText('Roll offset unit'), { target: { value: 'months' } });
    expect(onChange.mock.calls[0][0].roll_offset).toEqual({ value: 12, unit: 'months' });
  });

  it('emits a months value within 0..12', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.roll_offset = { value: 0, unit: 'months' };
    const { onChange } = renderForm({ value });
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '3' } });
    expect(onChange.mock.calls[0][0].roll_offset).toEqual({ value: 3, unit: 'months' });
    onChange.mockClear();
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '50' } });
    expect(onChange.mock.calls[0][0].roll_offset).toEqual({ value: 12, unit: 'months' });
  });

  it('reads a legacy bare-int roll_offset as {value, unit:days}', () => {
    // A persisted spec from before the unification carries a bare int.
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.roll_offset = 7;  // legacy days-only int
    renderForm({ value });
    expect(screen.getByLabelText('Roll offset value').value).toBe('7');
    expect(screen.getByLabelText('Roll offset unit').value).toBe('days');
  });

  // Clamp malformed / out-of-range value input into the unit's range (days
  // 0..365). Each case emits an integer value (never a string / float / negative).
  it.each([
    ['empty string', '', 0],
    ['negative', '-5', 0],
    ['non-numeric', 'abc', 0],
    ['above max', '999', 365],
    ['exactly max', '365', 365],
    ['three months out', '90', 90],
    ['in range', '7', 7],
  ])('clamps Roll offset value (%s) into 0..365 (days) as an int', (_label, raw, expected) => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: raw } });
    expect(onChange).toHaveBeenCalledOnce();
    const emitted = onChange.mock.calls[0][0].roll_offset;
    expect(emitted).toEqual({ value: expected, unit: 'days' });
    expect(Number.isInteger(emitted.value)).toBe(true);
  });

  it('respects allowedSelectionKinds — only by_moneyness rendered', () => {
    renderForm({ allowedSelectionKinds: ['by_moneyness'] });
    const sel = screen.getByLabelText('Selection criterion');
    const optionValues = Array.from(sel.querySelectorAll('option')).map((o) => o.value);
    expect(optionValues).toEqual(['by_moneyness']);
    expect(optionValues).not.toContain('by_strike');
    expect(optionValues).not.toContain('by_delta');
  });

  it('respects allowedMaturityKinds', () => {
    renderForm({ allowedMaturityKinds: ['fixed'] });
    const sel = screen.getByLabelText('Maturity rule');
    const optionValues = Array.from(sel.querySelectorAll('option')).map((o) => o.value);
    expect(optionValues).toEqual(['fixed']);
  });

  it('respects allowedStreams', () => {
    renderForm({ allowedStreams: ['iv', 'mid'] });
    const sel = screen.getByLabelText('Series');
    const optionValues = Array.from(sel.querySelectorAll('option')).map((o) => o.value);
    expect(optionValues).toEqual(['iv', 'mid']);
  });

  it('disabled=true disables every input', () => {
    renderForm({ disabled: true });
    const inputs = document.querySelectorAll(
      'input, select, textarea, fieldset',
    );
    inputs.forEach((el) => {
      expect(el.disabled).toBe(true);
    });
  });

  it('shows tautological validation when by_delta + stream=delta', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.selection = { kind: 'by_delta', target: 0.25, tolerance: 0.05, strict: false };
    value.stream = 'delta';
    renderForm({ value });
    const banner = screen.getByTestId('option-stream-validation');
    expect(banner.getAttribute('data-error-code')).toBe('TAUTOLOGICAL_OPTION_STREAM');
  });

  it('shows STREAM_UNAVAILABLE_FOR_ROOT when root has no greeks and stream is greek', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.collection = 'OPT_VIX';
    value.stream = 'gamma';
    renderForm({ value });
    const banner = screen.getByTestId('option-stream-validation');
    expect(banner.getAttribute('data-error-code')).toBe('STREAM_UNAVAILABLE_FOR_ROOT');
  });

  it('does not show validation banner for valid combos', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.stream = 'iv';
    renderForm({ value });
    expect(screen.queryByTestId('option-stream-validation')).toBeNull();
  });

  it('flips by_delta target sign when option_type changes from C to P', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.selection = { kind: 'by_delta', target: 0.25, tolerance: 0.05, strict: false };
    const { onChange } = renderForm({ value });
    fireEvent.click(screen.getByRole('radio', { name: 'Put' }));
    expect(onChange).toHaveBeenCalledOnce();
    const next = onChange.mock.calls[0][0];
    expect(next.option_type).toBe('P');
    expect(next.selection.target).toBeLessThan(0);
    expect(Math.abs(next.selection.target)).toBeCloseTo(0.25);
  });

  it('cycle "_any" maps to null in onChange payload', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Cycle'), { target: { value: '_any' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0].cycle).toBeNull();
  });

  it('cycle "M" maps to "M" in onChange payload', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Cycle'), { target: { value: 'M' } });
    expect(onChange.mock.calls[0][0].cycle).toBe('M');
  });
});

describe('<OptionStreamForm> — no Adjustment control', () => {
  // Option continuous series carry NO back-adjustment (ratio/difference are
  // ill-posed for option premia), so the form must NOT render an adjustment
  // selector — for any Series, including mid.
  it('never renders an Adjustment selector (mid)', () => {
    renderForm();
    expect(screen.queryByTestId('option-stream-adjustment')).toBeNull();
    expect(screen.queryByLabelText('Adjustment')).toBeNull();
  });

  it('never renders an Adjustment selector (non-mid)', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.stream = 'iv';
    renderForm({ value });
    expect(screen.queryByTestId('option-stream-adjustment')).toBeNull();
  });

  it('does not add an adjustment field when changing Series', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Series'), { target: { value: 'iv' } });
    expect(onChange).toHaveBeenCalledOnce();
    const next = onChange.mock.calls[0][0];
    expect(next.stream).toBe('iv');
    expect('adjustment' in next).toBe(false);
  });
});

describe('buildDefaultOptionStream', () => {
  it('builds a fully-shaped option_stream object', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS });
    expect(v).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      // Canonical default is W3 Friday (the real monthly cycle — every
      // month's 3rd Friday, PM-settled). ALL_CYCLES includes 'W3 Friday',
      // so the default picks it. Falls back to 'M', then first allowed.
      cycle: 'W3 Friday',
    });
    expect(v.maturity.kind).toBe('next_third_friday');
    expect(v.selection.kind).toBe('by_moneyness');
    expect(v.stream).toBe('mid');
    // Option streams carry no back-adjustment, so no `adjustment` field.
    expect('adjustment' in v).toBe(false);
  });

  it("falls back to allowedCycles[0] when 'M' is not allowed", () => {
    const v = buildDefaultOptionStream({
      availableRoots: ROOTS,
      allowedCycles: [null, 'W', 'Q'],
    });
    expect(v.cycle).toBeNull();
  });

  it('falls back to null when allowedCycles is empty', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS, allowedCycles: [] });
    expect(v.cycle).toBeNull();
  });

  it('falls back to empty collection when no roots are available', () => {
    const v = buildDefaultOptionStream({ availableRoots: [] });
    expect(v.collection).toBe('');
  });
});

// ── Root-scoped cycle dropdown (fix A) ─────────────────────────────────────
// The cycle dropdown is derived from the SELECTED root's real ``cycles`` tag-set
// (GET /api/options/roots), not the static ALL_CYCLES superset — so a root never
// offers a cycle it has no contracts for (which built an empty chain → HTTP 400).
describe('deriveCycleOptions (root-scoped cycles)', () => {
  const SP_CYCLES = ['M', 'W1 Friday', 'W2 Friday', 'W3 Friday', 'W4 Friday'];
  const BTC_CYCLES = ['D', 'M', 'Q', 'W'];

  const labels = (opts) => opts.map((o) => o.label);
  const values = (opts) => opts.map((o) => o.value);

  it('OPT_SP_500: Any + M + W1..W4 Friday + a SYNTHESISED generic Weekly, and NO Quarterly', () => {
    const opts = deriveCycleOptions(SP_CYCLES);
    expect(values(opts)).toEqual([
      null, 'M', 'W1 Friday', 'W2 Friday', 'W3 Friday', 'W4 Friday', 'W',
    ]);
    // Generic weekly is synthesised (root has W# Friday but no literal 'W').
    expect(labels(opts)).toContain(SYNTHETIC_WEEKLY_LABEL);
    // Phantom exclusion: NO Quarterly for an index root.
    expect(values(opts)).not.toContain('Q');
  });

  it('OPT_BTC: real set D/M/Q/W with the literal Weekly label, no synthetic duplicate', () => {
    const opts = deriveCycleOptions(BTC_CYCLES);
    expect(values(opts)).toEqual([null, 'D', 'M', 'Q', 'W']);
    // Exactly one 'W' entry, labelled with the literal (not synthetic) copy.
    expect(values(opts).filter((v) => v === 'W')).toHaveLength(1);
    const wOpt = opts.find((o) => o.value === 'W');
    expect(wOpt.label).toBe(CYCLE_LABELS.W);
    expect(wOpt.label).not.toBe(SYNTHETIC_WEEKLY_LABEL);
  });

  it('monthly-only roots (gold/FX/bonds/NASDAQ) offer just Any + Monthly', () => {
    const opts = deriveCycleOptions(['M']);
    expect(values(opts)).toEqual([null, 'M']);
    expect(values(opts)).not.toContain('W');
    expect(values(opts)).not.toContain('Q');
  });

  it('OPT_VIX (real literal W, no W# Friday): Any + M + W, no synthetic', () => {
    const opts = deriveCycleOptions(['M', 'W']);
    expect(values(opts)).toEqual([null, 'M', 'W']);
    expect(opts.find((o) => o.value === 'W').label).toBe(CYCLE_LABELS.W);
  });

  it('undefined / empty cycles falls back to the full static superset (legacy fixtures)', () => {
    const fromUndef = values(deriveCycleOptions(undefined));
    const fromEmpty = values(deriveCycleOptions([]));
    // Same fallback: Any + the historical superset.
    expect(fromUndef).toEqual(fromEmpty);
    expect(fromUndef).toContain(null);
    expect(fromUndef).toContain('W3 Friday');
    expect(fromUndef).toContain('Q');
  });

  it('allowedCycles applies as a FURTHER restriction on top of the root set', () => {
    const opts = deriveCycleOptions(SP_CYCLES, ['M', 'W3 Friday']);
    // Any dropped (null not in the restriction), only the two survive.
    expect(values(opts)).toEqual(['M', 'W3 Friday']);
  });

  it('allowedCycles including null keeps the Any sentinel', () => {
    const opts = deriveCycleOptions(SP_CYCLES, [null, 'M']);
    expect(values(opts)).toEqual([null, 'M']);
  });
});

describe('pickDefaultCycle', () => {
  it('prefers W3 Friday over M', () => {
    expect(pickDefaultCycle(deriveCycleOptions(['M', 'W3 Friday']))).toBe('W3 Friday');
  });
  it('falls back to M when no W3 Friday', () => {
    expect(pickDefaultCycle(deriveCycleOptions(['D', 'M', 'Q', 'W']))).toBe('M');
  });
  it('falls back to Any (null) when neither W3 nor M is present', () => {
    expect(pickDefaultCycle(deriveCycleOptions(['D', 'Q']))).toBeNull();
  });
  it('falls back to the first concrete cycle when Any is not offered', () => {
    expect(pickDefaultCycle(deriveCycleOptions(['D', 'Q'], ['D', 'Q']))).toBe('D');
  });
});

describe('<OptionStreamForm> root-scoped cycle dropdown (render)', () => {
  const ROOTS_WITH_CYCLES = [
    {
      collection: 'OPT_SP_500',
      root_label: 'SP 500',
      has_greeks: true,
      cycles: ['M', 'W1 Friday', 'W2 Friday', 'W3 Friday', 'W4 Friday'],
    },
    {
      collection: 'OPT_BTC',
      root_label: 'BTC',
      has_greeks: true,
      cycles: ['D', 'M', 'Q', 'W'],
    },
    {
      collection: 'OPT_GOLD',
      root_label: 'Gold',
      has_greeks: true,
      cycles: ['M'],
    },
  ];

  const cycleOptionValues = () => {
    const sel = screen.getByLabelText('Cycle');
    return Array.from(sel.querySelectorAll('option')).map((o) => o.value);
  };
  const cycleOptionLabels = () => {
    const sel = screen.getByLabelText('Cycle');
    return Array.from(sel.querySelectorAll('option')).map((o) => o.textContent);
  };

  it('OPT_SP_500 shows Any/M/W1-4 Friday + synthetic Weekly and NOT Quarterly', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES });
    render(<OptionStreamForm value={value} onChange={vi.fn()} availableRoots={ROOTS_WITH_CYCLES} />);
    expect(cycleOptionValues()).toEqual([
      '_any', 'M', 'W1 Friday', 'W2 Friday', 'W3 Friday', 'W4 Friday', 'W',
    ]);
    expect(cycleOptionLabels()).toContain(SYNTHETIC_WEEKLY_LABEL);
    expect(cycleOptionValues()).not.toContain('Q');
  });

  it('OPT_BTC shows its real D/M/Q/W set (no synthetic dup)', () => {
    // Use a cycle that IS in BTC's real set ('M') so this asserts the pure
    // derived set — otherwise the SP500 default 'W3 Friday' rides along and is
    // (correctly) surfaced as an "(unavailable)" extra, which is a different case.
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_BTC', cycle: 'M' };
    render(<OptionStreamForm value={value} onChange={vi.fn()} availableRoots={ROOTS_WITH_CYCLES} />);
    expect(cycleOptionValues()).toEqual(['_any', 'D', 'M', 'Q', 'W']);
  });

  it('coerces an invalid cycle to the root default when switching root (Q on BTC → Gold)', () => {
    const onChange = vi.fn();
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_BTC', cycle: 'Q' };
    render(<OptionStreamForm value={value} onChange={onChange} availableRoots={ROOTS_WITH_CYCLES} />);
    fireEvent.change(screen.getByLabelText('Root'), { target: { value: 'OPT_GOLD' } });
    expect(onChange).toHaveBeenCalledOnce();
    const next = onChange.mock.calls[0][0];
    expect(next.collection).toBe('OPT_GOLD');
    // Gold only has 'M' → 'Q' is invalid → snap to the root default (M).
    expect(next.cycle).toBe('M');
  });

  it('keeps a still-valid cycle when switching root (M on BTC → Gold)', () => {
    const onChange = vi.fn();
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_BTC', cycle: 'M' };
    render(<OptionStreamForm value={value} onChange={onChange} availableRoots={ROOTS_WITH_CYCLES} />);
    fireEvent.change(screen.getByLabelText('Root'), { target: { value: 'OPT_GOLD' } });
    const next = onChange.mock.calls[0][0];
    expect(next.cycle).toBe('M');
  });

  it('buildDefaultOptionStream picks W3 Friday for OPT_SP_500 real cycles', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES });
    expect(v.collection).toBe('OPT_SP_500');
    expect(v.cycle).toBe('W3 Friday');
  });

  // ── Truthful display of a stale / out-of-list persisted cycle ──────────────
  // A legacy signal saved with a cycle the selected root no longer offers (e.g.
  // 'Q' on OPT_SP_500) must remain TRUTHFULLY visible: shown as an extra,
  // clearly-labelled "(unavailable)" option — NEVER silently coerced on mount.
  it('shows a persisted out-of-list cycle as an extra "(unavailable)" option', () => {
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_SP_500', cycle: 'Q' };
    render(<OptionStreamForm value={value} onChange={vi.fn()} availableRoots={ROOTS_WITH_CYCLES} />);
    // The out-of-list value is present as an option and clearly flagged.
    expect(cycleOptionValues()).toContain('Q');
    expect(cycleOptionLabels().some((l) => /Q.*\(unavailable\)/.test(l))).toBe(true);
    // The <select> shows exactly what was saved.
    expect(screen.getByLabelText('Cycle').value).toBe('Q');
  });

  it('does NOT silently mutate a persisted out-of-list cycle on mount', () => {
    const onChange = vi.fn();
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_SP_500', cycle: 'Q' };
    render(<OptionStreamForm value={value} onChange={onChange} availableRoots={ROOTS_WITH_CYCLES} />);
    // Mount must not coerce the saved value — the user re-picks consciously.
    expect(onChange).not.toHaveBeenCalled();
  });

  it('read-only (disabled) mode preserves the out-of-list value and never mutates it', () => {
    const onChange = vi.fn();
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_SP_500', cycle: 'Q' };
    render(<OptionStreamForm value={value} onChange={onChange} availableRoots={ROOTS_WITH_CYCLES} disabled />);
    const sel = screen.getByLabelText('Cycle');
    expect(sel.value).toBe('Q');
    expect(sel.disabled).toBe(true);
    expect(cycleOptionLabels().some((l) => /Q.*\(unavailable\)/.test(l))).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('does NOT add an extra option when the persisted cycle is in-list', () => {
    const value = { ...buildDefaultOptionStream({ availableRoots: ROOTS_WITH_CYCLES }), collection: 'OPT_BTC', cycle: 'M' };
    render(<OptionStreamForm value={value} onChange={vi.fn()} availableRoots={ROOTS_WITH_CYCLES} />);
    // OPT_BTC really has M → the dropdown is its plain real set, no "(unavailable)".
    expect(cycleOptionValues()).toEqual(['_any', 'D', 'M', 'Q', 'W']);
    expect(cycleOptionLabels().some((l) => /\(unavailable\)/.test(l))).toBe(false);
  });
});

describe('validateOptionStream', () => {
  it('returns null on a fully valid value', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS });
    v.stream = 'mid';
    expect(validateOptionStream(v, ROOTS)).toBeNull();
  });

  it('returns NO_ROOT when collection is empty', () => {
    const v = buildDefaultOptionStream({ availableRoots: [] });
    expect(validateOptionStream(v, []).code).toBe('NO_ROOT');
  });

  it('returns STREAM_UNAVAILABLE_FOR_ROOT for greek stream on greek-less root', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS });
    v.collection = 'OPT_VIX';
    v.stream = 'theta';
    expect(validateOptionStream(v, ROOTS).code).toBe('STREAM_UNAVAILABLE_FOR_ROOT');
  });

  it('returns TAUTOLOGICAL_OPTION_STREAM for by_delta + delta', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS });
    v.selection = { kind: 'by_delta', target: 0.25, tolerance: 0.05, strict: false };
    v.stream = 'delta';
    expect(validateOptionStream(v, ROOTS).code).toBe('TAUTOLOGICAL_OPTION_STREAM');
  });
});

// ── Select-and-hold (fixed-contract dollar P&L) controls — SIGNALS-only ──
describe('<OptionStreamForm> hold controls (showHoldControls)', () => {
  it('does NOT render the hold controls by default (Data/Portfolio pickers)', () => {
    renderForm();
    expect(screen.queryByTestId('hold-between-rolls')).toBeNull();
    expect(screen.queryByTestId('nav-times')).toBeNull();
  });

  it('renders the hold toggle when showHoldControls is true; nav_times hidden until on', () => {
    renderForm({ showHoldControls: true });
    const toggle = screen.getByTestId('hold-between-rolls');
    expect(toggle).toBeTruthy();
    expect(toggle.checked).toBe(false);
    // nav_times input only appears once hold is enabled.
    expect(screen.queryByTestId('nav-times')).toBeNull();
  });

  it('enabling hold emits hold_between_rolls=true and seeds nav_times=1.0', () => {
    const onChange = vi.fn();
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    renderForm({ value, onChange, showHoldControls: true });
    fireEvent.click(screen.getByTestId('hold-between-rolls'));
    expect(onChange).toHaveBeenCalledTimes(1);
    const emitted = onChange.mock.calls[0][0];
    expect(emitted.hold_between_rolls).toBe(true);
    expect(emitted.nav_times).toBe(1.0);
  });

  it('shows the nav_times input as the RAW multiplier when hold is on and edits it', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 1.0,
    };
    renderForm({ value, onChange, showHoldControls: true });
    const navInput = screen.getByTestId('nav-times');
    expect(navInput).toBeTruthy();
    // The wire multiplier 1.0 (unlevered) DISPLAYS as the raw "1" (not 100).
    expect(String(navInput.value)).toBe('1');
    // Typing 2 stores nav_times = 2 VERBATIM (NOT 200, NOT 0.02).
    fireEvent.change(navInput, { target: { value: '2' } });
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.nav_times).toBe(2);
    expect(emitted.hold_between_rolls).toBe(true);
  });

  it('a non-positive / non-numeric nav_times falls back to 1.0 (backend also guards)', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 2.0,
    };
    renderForm({ value, onChange, showHoldControls: true });
    fireEvent.change(screen.getByTestId('nav-times'), { target: { value: '0' } });
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.nav_times).toBe(1.0);
  });

  it('turning hold OFF preserves the other fields and clears the toggle', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 2.5,
    };
    renderForm({ value, onChange, showHoldControls: true });
    fireEvent.click(screen.getByTestId('hold-between-rolls'));
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.hold_between_rolls).toBe(false);
    // Other option-stream fields are untouched by the toggle.
    expect(emitted.collection).toBe(value.collection);
    expect(emitted.stream).toBe(value.stream);
  });
});

// ── Portfolio ON-only: hold is required for an option price leg (no toggle) ──
describe('<OptionStreamForm> holdRequired (portfolio ON-only)', () => {
  it('renders NO on/off toggle, but shows the held note + nav_times + wipeout hint', () => {
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 1.0,
    };
    renderForm({ value, holdRequired: true });
    // A rolled option price leg is ALWAYS held — there is no interactive off.
    expect(screen.queryByTestId('hold-between-rolls')).toBeNull();
    expect(screen.getByTestId('hold-required-note')).toBeTruthy();
    // nav_times is always visible (not gated behind a toggle) + a wipeout hint.
    expect(screen.getByTestId('nav-times')).toBeTruthy();
    expect(screen.getByTestId('nav-hint')).toBeTruthy();
  });

  it('defaults cycle to "M" on mount', () => {
    // The hold flag is forced on by AddHoldingModal (the single authority), NOT by
    // this form's one-shot effect — which now only defaults the cycle to 'M'.
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: false,
      cycle: 'W3 Friday',
    };
    renderForm({ value, onChange, holdRequired: true });
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.cycle).toBe('M');
  });

  it('keeps nav_times editable as the RAW multiplier (always shown, no toggle gating)', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 1.0,
    };
    renderForm({ value, onChange, holdRequired: true });
    // Enter 0.5 → stored VERBATIM as 0.5 (no ×100/÷100 conversion).
    fireEvent.change(screen.getByTestId('nav-times'), { target: { value: '0.5' } });
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.nav_times).toBe(0.5);
  });

  it('displays the stored multiplier verbatim (0.0045 → "0.0045", no ×100)', () => {
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 0.0045,
    };
    renderForm({ value, holdRequired: true });
    expect(String(screen.getByTestId('nav-times').value)).toBe('0.0045');
  });
});

// ── Sizing mode (futures-notional) — shared across signals + portfolio ──────
describe('<OptionStreamForm> sizing mode (premium vs futures notional)', () => {
  const holdValue = () => ({
    ...buildDefaultOptionStream({ availableRoots: ROOTS }),
    hold_between_rolls: true,
    nav_times: 1.0,
  });

  it('defaults the sizing-mode select to premium_notional; no reference dropdown; leverage readout shown', () => {
    renderForm({ value: holdValue(), showHoldControls: true });
    const sel = screen.getByTestId('sizing-mode');
    expect(sel.value).toBe('premium_notional');
    // Futures-reference dropdown is absent in percentage mode.
    expect(screen.queryByTestId('futures-reference')).toBeNull();
    expect(screen.queryByTestId('futures-notional-help')).toBeNull();
    // The premium-notional leverage readout renders (fallback hint here, since
    // the test ROOTS carry no last_trade_date / referenceDate to probe).
    expect(screen.getByTestId('nav-hint')).toBeTruthy();
  });

  it('offers exactly two sizing-mode options with the documented labels', () => {
    renderForm({ value: holdValue(), showHoldControls: true });
    const opts = Array.from(screen.getByTestId('sizing-mode').querySelectorAll('option'));
    expect(opts.map((o) => o.value)).toEqual(['premium_notional', 'futures_notional']);
    expect(opts.map((o) => o.textContent)).toEqual([
      SIZING_MODE_LABELS.premium_notional,
      SIZING_MODE_LABELS.futures_notional,
    ]);
  });

  it('switching to Futures notional emits sizing_mode + seeds futures_reference=nearest_on_or_after', () => {
    const onChange = vi.fn();
    renderForm({ value: holdValue(), onChange, showHoldControls: true });
    fireEvent.change(screen.getByTestId('sizing-mode'), { target: { value: 'futures_notional' } });
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.sizing_mode).toBe('futures_notional');
    expect(emitted.futures_reference).toBe('nearest_on_or_after');
  });

  it('in futures mode: reference dropdown (2 options, NO continuous_front) + helper + nav_times; leverage readout HIDDEN', () => {
    const value = { ...holdValue(), sizing_mode: 'futures_notional', futures_reference: 'nearest_on_or_after' };
    renderForm({ value, showHoldControls: true });
    const ref = screen.getByTestId('futures-reference');
    const refOpts = Array.from(ref.querySelectorAll('option')).map((o) => o.value);
    expect(refOpts).toEqual(['nearest_on_or_after', 'nearest_abs']);
    expect(refOpts).not.toContain('continuous_front');
    expect(screen.getByTestId('futures-notional-help')).toBeTruthy();
    // nav_times stays exposed in both modes.
    expect(screen.getByTestId('nav-times')).toBeTruthy();
    // The premium-notional readout must NOT render (neither the hint nor the data group).
    expect(screen.queryByTestId('nav-hint')).toBeNull();
    expect(screen.queryByTestId('lev-readout-group')).toBeNull();
  });

  it('changing the futures reference emits futures_reference=nearest_abs', () => {
    const onChange = vi.fn();
    const value = { ...holdValue(), sizing_mode: 'futures_notional', futures_reference: 'nearest_on_or_after' };
    renderForm({ value, onChange, showHoldControls: true });
    fireEvent.change(screen.getByTestId('futures-reference'), { target: { value: 'nearest_abs' } });
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.futures_reference).toBe('nearest_abs');
  });

  it('renders the sizing control in the PORTFOLIO holdRequired branch (percentage mode)', () => {
    renderForm({ value: holdValue(), holdRequired: true });
    expect(screen.getByTestId('sizing-mode').value).toBe('premium_notional');
    expect(screen.queryByTestId('futures-reference')).toBeNull();
  });

  it('futures mode works in the PORTFOLIO holdRequired branch too (readout hidden)', () => {
    const value = { ...holdValue(), sizing_mode: 'futures_notional' };
    renderForm({ value, holdRequired: true });
    expect(screen.getByTestId('futures-reference')).toBeTruthy();
    expect(screen.getByTestId('futures-notional-help')).toBeTruthy();
    expect(screen.queryByTestId('nav-hint')).toBeNull();
  });

  it('a never-touched default leg carries NO sizing_mode / futures_reference key (byte-identical serialisation)', () => {
    const v = buildDefaultOptionStream({ availableRoots: ROOTS });
    expect('sizing_mode' in v).toBe(false);
    expect('futures_reference' in v).toBe(false);
  });

  it('the sizing control is NOT shown when hold is off (signals default)', () => {
    renderForm({ showHoldControls: true }); // hold defaults off
    expect(screen.queryByTestId('sizing-mode')).toBeNull();
  });
});

// ── Size field is a raw multiplier (no ×100 / ÷100), mode-aware label ───────
describe('nav_times Size multiplier presentation', () => {
  const holdValue = (nav = 1.0) => ({
    ...buildDefaultOptionStream({ availableRoots: ROOTS }),
    hold_between_rolls: true,
    nav_times: nav,
  });

  it('default nav_times 1.0 displays as the raw "1"', () => {
    renderForm({ value: holdValue(1.0), showHoldControls: true });
    expect(String(screen.getByTestId('nav-times').value)).toBe('1');
  });

  it('typing 2 stores nav_times = 2 verbatim (NOT 0.02, NOT 200)', () => {
    const onChange = vi.fn();
    renderForm({ value: holdValue(1.0), onChange, showHoldControls: true });
    fireEvent.change(screen.getByTestId('nav-times'), { target: { value: '2' } });
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.nav_times).toBe(2);
  });

  it('supports leverage > 1 and fractions < 1 verbatim', () => {
    const onChange = vi.fn();
    renderForm({ value: holdValue(1.0), onChange, showHoldControls: true });
    const input = screen.getByTestId('nav-times');
    fireEvent.change(input, { target: { value: '3.5' } });
    expect(onChange.mock.calls[onChange.mock.calls.length - 1][0].nav_times).toBe(3.5);
    fireEvent.change(input, { target: { value: '0.25' } });
    expect(onChange.mock.calls[onChange.mock.calls.length - 1][0].nav_times).toBe(0.25);
  });

  it('labels the Size field as a factor — premium mode by default, futures mode when set', () => {
    // Premium-notional (default) mode.
    const { unmount } = renderForm({ value: holdValue(1.0), showHoldControls: true });
    let input = screen.getByTestId('nav-times');
    expect(input.getAttribute('aria-label')).toBe(SIZE_LABEL_PREMIUM);
    expect(SIZE_LABEL_PREMIUM).toMatch(/×/); // reads as a multiplier, not a %
    expect(SIZE_LABEL_PREMIUM).not.toMatch(/%/);
    unmount();
    // Futures-notional mode.
    renderForm({
      value: { ...holdValue(1.0), sizing_mode: 'futures_notional' },
      showHoldControls: true,
    });
    input = screen.getByTestId('nav-times');
    expect(input.getAttribute('aria-label')).toBe(SIZE_LABEL_FUTURES);
    expect(SIZE_LABEL_FUTURES).toMatch(/×/);
    expect(SIZE_LABEL_FUTURES).not.toMatch(/%/);
  });
});
