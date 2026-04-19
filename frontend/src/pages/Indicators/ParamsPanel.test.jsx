// @vitest-environment jsdom
//
// Focused tests for the "Show in separate panel below" toggle added
// in the ownPanel feature. We don't re-cover the full ParamsPanel
// behaviour here — just the new checkbox and its readonly/empty guards.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ParamsPanel from './ParamsPanel';

afterEach(() => {
  cleanup();
});

function baseProps(overrides = {}) {
  return {
    indicator: {
      id: 'u1',
      name: 'My ind',
      code: "def compute(series):\n    return series['close']",
      params: {},
      seriesMap: {},
      readonly: false,
    },
    paramsSpec: [],
    seriesLabels: [],
    onParamChange: vi.fn(),
    onSeriesSave: vi.fn(),
    onRun: vi.fn(),
    running: false,
    canRun: false,
    runDisabledReason: null,
    defaultCollection: null,
    ownPanel: false,
    onOwnPanelChange: vi.fn(),
    ...overrides,
  };
}

describe('<ParamsPanel> — ownPanel checkbox', () => {
  it('renders the checkbox labelled "Show in separate panel below"', () => {
    render(<ParamsPanel {...baseProps()} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb).toBeTruthy();
    expect(cb.checked).toBe(false);
    expect(screen.getByText(/show in separate panel below/i)).toBeTruthy();
  });

  it('reflects the current ownPanel value', () => {
    render(<ParamsPanel {...baseProps({ ownPanel: true })} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb.checked).toBe(true);
  });

  it('calls onOwnPanelChange with the new checked state when toggled', () => {
    const onOwnPanelChange = vi.fn();
    render(<ParamsPanel {...baseProps({ ownPanel: false, onOwnPanelChange })} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    fireEvent.click(cb);
    expect(onOwnPanelChange).toHaveBeenCalledTimes(1);
    expect(onOwnPanelChange).toHaveBeenCalledWith(true);
  });

  it('is disabled (native attr) and does not fire callback when indicator is readonly', () => {
    const onOwnPanelChange = vi.fn();
    render(
      <ParamsPanel
        {...baseProps({
          indicator: { ...baseProps().indicator, readonly: true },
          ownPanel: true,
          onOwnPanelChange,
        })}
      />,
    );
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb.disabled).toBe(true);
    // Even if the click somehow fires, our onChange handler must early-return.
    fireEvent.click(cb);
    expect(onOwnPanelChange).not.toHaveBeenCalled();
  });

  it('is disabled when no indicator is selected', () => {
    render(<ParamsPanel {...baseProps({ indicator: null })} />);
    const cb = screen.getByRole('checkbox', { name: /show in separate panel below/i });
    expect(cb.disabled).toBe(true);
  });
});
