// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });

import IndicatorParamsOverride, {
  writeOverrides,
  coerceParamInput,
  effectiveParamValue,
} from './IndicatorParamsOverride';

const SPX_INPUT = {
  id: 'X',
  instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' },
};

function baseOperand(overrides = {}) {
  return {
    kind: 'indicator',
    indicator_id: 'ind-1',
    input_id: 'X',
    output: 'default',
    params_override: null,
    series_override: null,
    ...overrides,
  };
}

function zeroParamIndicator() {
  return {
    id: 'ind-0',
    name: 'Zero Params',
    params: {},
    seriesMap: {},
    code: "def compute(series):\n    return series['price']",
  };
}

function oneParamIndicator() {
  return {
    id: 'ind-1',
    name: 'One Param',
    params: { window: 20 },
    seriesMap: {},
    code: "def compute(series, window: int = 20):\n    return series['price']",
  };
}

function twoParamIndicator() {
  return {
    id: 'ind-2',
    name: 'Two Params',
    params: { window: 20, alpha: 0.5 },
    seriesMap: {},
    code: "def compute(series, window: int = 20, alpha: float = 0.5):\n    return series['price']",
  };
}

describe('IndicatorParamsOverride — tier selection (ORDERS bullet #4)', () => {
  it('0 editable controls → non-clickable "No parameters" tag', () => {
    const onOperandChange = vi.fn();
    render(
      <IndicatorParamsOverride
        indicator={zeroParamIndicator()}
        operand={baseOperand()}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    const tag = screen.getByTestId('indicator-override-no-params');
    expect(tag).toBeDefined();
    expect(tag.textContent).toMatch(/no parameters/i);
    // Tier 2 / Tier 3 widgets should NOT be present.
    expect(screen.queryByTestId('indicator-override-toggle')).toBeNull();
    expect(screen.queryByTestId('indicator-override-inline')).toBeNull();
  });

  it('1 editable param → inline <name>: <value> editor, no dropdown', () => {
    const onOperandChange = vi.fn();
    render(
      <IndicatorParamsOverride
        indicator={oneParamIndicator()}
        operand={baseOperand()}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    expect(screen.queryByTestId('indicator-override-toggle')).toBeNull();
    const wrap = screen.getByTestId('indicator-override-inline');
    expect(wrap.textContent).toContain('window');
    const input = screen.getByTestId('indicator-override-inline-window');
    // Default from indicator.params.window (20)
    expect(input.value).toBe('20');
  });

  it('inline 1-param editor writes to params_override (same storage field as dropdown)', () => {
    const onOperandChange = vi.fn();
    render(
      <IndicatorParamsOverride
        indicator={oneParamIndicator()}
        operand={baseOperand()}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    const input = screen.getByTestId('indicator-override-inline-window');
    fireEvent.change(input, { target: { value: '50' } });
    const next = onOperandChange.mock.calls.pop()[0];
    expect(next.params_override).toEqual({ window: 50 });
    expect(next.series_override).toBeNull();
    // Operand identity is preserved otherwise.
    expect(next.kind).toBe('indicator');
    expect(next.indicator_id).toBe('ind-1');
  });

  it('2+ editable params → collapsible "Parameters" dropdown (Tier 3)', () => {
    const onOperandChange = vi.fn();
    render(
      <IndicatorParamsOverride
        indicator={twoParamIndicator()}
        operand={baseOperand()}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    expect(screen.queryByTestId('indicator-override-inline')).toBeNull();
    expect(screen.queryByTestId('indicator-override-no-params')).toBeNull();
    const toggle = screen.getByTestId('indicator-override-toggle');
    expect(toggle).toBeDefined();
    // Dropdown is collapsed by default.
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
  });

  it('Tier 3 dropdown writes to params_override via the same writeOverrides path', () => {
    const onOperandChange = vi.fn();
    render(
      <IndicatorParamsOverride
        indicator={twoParamIndicator()}
        operand={baseOperand()}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    fireEvent.click(screen.getByTestId('indicator-override-toggle'));
    const input = screen.getByTestId('override-param-window');
    fireEvent.change(input, { target: { value: '30' } });
    const next = onOperandChange.mock.calls.pop()[0];
    expect(next.params_override).toEqual({ window: 30 });
  });

  it('1→2 param transition does NOT orphan params_override state', () => {
    // CRITICAL ORDERS bullet #4 requirement: flipping an indicator's
    // params list between 1 and 2 must not orphan state. Both branches
    // write to the SAME field (params_override).
    const onOperandChange = vi.fn();
    const initialOperand = baseOperand({
      params_override: { window: 42 },
    });
    // First render: 1-param indicator → inline editor reads window=42.
    const { rerender } = render(
      <IndicatorParamsOverride
        indicator={oneParamIndicator()}
        operand={initialOperand}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    const inlineInput = screen.getByTestId('indicator-override-inline-window');
    expect(inlineInput.value).toBe('42');

    // Now flip the indicator to 2 params — params_override must survive.
    rerender(
      <IndicatorParamsOverride
        indicator={twoParamIndicator()}
        operand={initialOperand}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    // Inline editor is gone; dropdown is present.
    expect(screen.queryByTestId('indicator-override-inline')).toBeNull();
    const toggle = screen.getByTestId('indicator-override-toggle');
    expect(toggle).toBeDefined();
    // Open the dropdown; the override for window must still be 42.
    fireEvent.click(toggle);
    const dropdownInput = screen.getByTestId('override-param-window');
    expect(dropdownInput.value).toBe('42');

    // And flipping BACK to 1 param preserves it too.
    rerender(
      <IndicatorParamsOverride
        indicator={oneParamIndicator()}
        operand={initialOperand}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    const inlineAgain = screen.getByTestId('indicator-override-inline-window');
    expect(inlineAgain.value).toBe('42');
  });

  it('inline editor → dropdown edit chains produce cumulatively valid operand state', () => {
    const onOperandChange = vi.fn();
    let operand = baseOperand();

    // Start 1-param.
    const { rerender } = render(
      <IndicatorParamsOverride
        indicator={oneParamIndicator()}
        operand={operand}
        inputs={[SPX_INPUT]}
        onOperandChange={(next) => { operand = next; onOperandChange(next); }}
      />,
    );
    fireEvent.change(screen.getByTestId('indicator-override-inline-window'), {
      target: { value: '7' },
    });
    // Rerender with updated operand and flipped indicator.
    rerender(
      <IndicatorParamsOverride
        indicator={twoParamIndicator()}
        operand={operand}
        inputs={[SPX_INPUT]}
        onOperandChange={(next) => { operand = next; onOperandChange(next); }}
      />,
    );
    fireEvent.click(screen.getByTestId('indicator-override-toggle'));
    fireEvent.change(screen.getByTestId('override-param-alpha'), {
      target: { value: '0.9' },
    });
    expect(operand.params_override).toEqual({ window: 7, alpha: 0.9 });
  });

  it('clearing the inline input removes the override (collapses to null if empty)', () => {
    const onOperandChange = vi.fn();
    render(
      <IndicatorParamsOverride
        indicator={oneParamIndicator()}
        operand={baseOperand({ params_override: { window: 42 } })}
        inputs={[SPX_INPUT]}
        onOperandChange={onOperandChange}
      />,
    );
    const input = screen.getByTestId('indicator-override-inline-window');
    fireEvent.change(input, { target: { value: '' } });
    const next = onOperandChange.mock.calls.pop()[0];
    expect(next.params_override).toBeNull();
  });
});

describe('IndicatorParamsOverride — pure helpers', () => {
  it('writeOverrides collapses empty maps to null', () => {
    const op = { kind: 'indicator', indicator_id: 'a' };
    expect(writeOverrides(op, {}, {}))
      .toMatchObject({ params_override: null, series_override: null });
    expect(writeOverrides(op, { x: 1 }, {}))
      .toMatchObject({ params_override: { x: 1 }, series_override: null });
    expect(writeOverrides(op, {}, { y: 'Z' }))
      .toMatchObject({ params_override: null, series_override: { y: 'Z' } });
  });

  it('coerceParamInput returns undefined on empty/invalid, coerces otherwise', () => {
    expect(coerceParamInput('int', '')).toBeUndefined();
    expect(coerceParamInput('int', 'abc')).toBeUndefined();
    expect(coerceParamInput('int', '42')).toBe(42);
    expect(coerceParamInput('float', '0.5')).toBe(0.5);
    expect(coerceParamInput('bool', true)).toBe(true);
    expect(coerceParamInput('bool', false)).toBe(false);
    expect(coerceParamInput('str', 'hello')).toBe('hello');
  });

  it('effectiveParamValue: override > indicator default > parser default', () => {
    const spec = { name: 'window', default: 10, type: 'int' };
    expect(effectiveParamValue(spec, {}, {})).toBe(10);
    expect(effectiveParamValue(spec, { window: 20 }, {})).toBe(20);
    expect(effectiveParamValue(spec, { window: 20 }, { window: 30 })).toBe(30);
  });
});
