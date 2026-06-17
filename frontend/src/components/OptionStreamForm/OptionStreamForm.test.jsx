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

describe('<OptionStreamForm> — Adjustment selector (MID only)', () => {
  it('renders the Adjustment selector when Series is mid (the default)', () => {
    renderForm();
    const adj = screen.getByTestId('option-stream-adjustment');
    expect(adj).toBeTruthy();
    // none / ratio / difference — mirrors the futures continuous picker.
    const optionValues = Array.from(adj.querySelectorAll('option')).map((o) => o.value);
    expect(optionValues).toEqual(['none', 'ratio', 'difference']);
    // Default value is "none".
    expect(adj.value).toBe('none');
  });

  it('hides the Adjustment selector when Series is not mid', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.stream = 'iv';
    renderForm({ value });
    expect(screen.queryByTestId('option-stream-adjustment')).toBeNull();
  });

  it('emits the chosen adjustment when Series is mid', () => {
    const { onChange } = renderForm();
    fireEvent.change(screen.getByTestId('option-stream-adjustment'), {
      target: { value: 'ratio' },
    });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange.mock.calls[0][0].adjustment).toBe('ratio');
    expect(onChange.mock.calls[0][0].stream).toBe('mid');
  });

  it('resets adjustment to "none" when Series switches away from mid', () => {
    // Start on mid with a non-default adjustment, then switch to iv.
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.adjustment = 'difference';
    const { onChange } = renderForm({ value });
    fireEvent.change(screen.getByLabelText('Series'), { target: { value: 'iv' } });
    expect(onChange).toHaveBeenCalledOnce();
    const next = onChange.mock.calls[0][0];
    expect(next.stream).toBe('iv');
    expect(next.adjustment).toBe('none');
  });

  it('keeps the current adjustment when staying on mid', () => {
    const value = buildDefaultOptionStream({ availableRoots: ROOTS });
    value.adjustment = 'ratio';
    renderForm({ value });
    // Selecting mid again (the current value) must not be coerced; the
    // rendered control still reflects ratio.
    expect(screen.getByTestId('option-stream-adjustment').value).toBe('ratio');
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
    expect(v.adjustment).toBe('none');
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
