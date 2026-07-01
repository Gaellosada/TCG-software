// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import OptionStreamForm, {
  buildDefaultOptionStream,
  validateOptionStream,
  MID_TOOLTIP,
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

  it('emits roll_offset {value, unit:days} on value change, clamped 0..30', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '5' } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0]).toMatchObject({
      roll_offset: { value: 5, unit: 'days' },
    });
    onChange.mockClear();
    fireEvent.change(screen.getByLabelText('Roll offset value'), { target: { value: '99' } });
    expect(onChange.mock.calls[0][0].roll_offset).toEqual({ value: 30, unit: 'days' });
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
  // 0..30). Each case emits an integer value (never a string / float / negative).
  it.each([
    ['empty string', '', 0],
    ['negative', '-5', 0],
    ['non-numeric', 'abc', 0],
    ['above max', '99', 30],
    ['exactly max', '30', 30],
    ['in range', '7', 7],
  ])('clamps Roll offset value (%s) into 0..30 (days) as an int', (_label, raw, expected) => {
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

  it('shows the nav_times input when hold is already on and edits it', () => {
    const onChange = vi.fn();
    const value = {
      ...buildDefaultOptionStream({ availableRoots: ROOTS }),
      hold_between_rolls: true,
      nav_times: 1.0,
    };
    renderForm({ value, onChange, showHoldControls: true });
    const navInput = screen.getByTestId('nav-times');
    expect(navInput).toBeTruthy();
    expect(String(navInput.value)).toBe('1');
    // nav_times may exceed 1 (leverage the premium notional).
    fireEvent.change(navInput, { target: { value: '2.5' } });
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(emitted.nav_times).toBe(2.5);
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
