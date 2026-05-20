import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import RiskFreeRateInput from './RiskFreeRateInput';

afterEach(() => cleanup());

describe('RiskFreeRateInput', () => {
  it('renders the value, % unit, and aria-label', () => {
    render(
      <RiskFreeRateInput
        valuePct="4.50"
        onChange={() => {}}
        ariaLabel="rfr-render"
      />,
    );
    const input = screen.getByLabelText('rfr-render');
    expect(input.value).toBe('4.50');
    expect(input.getAttribute('type')).toBe('number');
    expect(input.getAttribute('step')).toBe('0.01');
    expect(input.getAttribute('min')).toBe('0');
    expect(screen.getByText('%')).toBeTruthy();
  });

  it('renders an inline label when provided', () => {
    render(
      <RiskFreeRateInput
        valuePct="0.00"
        onChange={() => {}}
        ariaLabel="rfr-with-label"
        label="Risk-free rate:"
      />,
    );
    expect(screen.getByText('Risk-free rate:')).toBeTruthy();
  });

  it('omits the inline label when not provided', () => {
    render(
      <RiskFreeRateInput
        valuePct="0.00"
        onChange={() => {}}
        ariaLabel="rfr-no-label"
      />,
    );
    expect(screen.queryByText('Risk-free rate:')).toBeNull();
  });

  it('fires onChange with the event when the user types', () => {
    const onChange = vi.fn();
    render(
      <RiskFreeRateInput
        valuePct="4.00"
        onChange={onChange}
        ariaLabel="rfr-onchange"
      />,
    );
    fireEvent.change(screen.getByLabelText('rfr-onchange'), {
      target: { value: '5.5' },
    });
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
