// @vitest-environment jsdom

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DataSourceSelector from './DataSourceSelector';
import { V2_LIMITATIONS } from '../lib/dataSource';

describe('DataSourceSelector', () => {
  it('offers both databases and reflects the current value', () => {
    render(<DataSourceSelector value="v1" onChange={() => {}} />);
    const select = screen.getByTestId('data-source-select');
    expect(select.value).toBe('v1');
    expect(screen.getByRole('option', { name: 'Database v1' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Database v2' })).toBeTruthy();
  });

  it('does NOT render the v2 limitation notes on v1', () => {
    render(<DataSourceSelector value="v1" onChange={() => {}} />);
    expect(screen.queryByTestId('data-source-v2-notes')).toBeNull();
  });

  it('renders every measured v2 limitation when v2 is selected', () => {
    render(<DataSourceSelector value="v2" onChange={() => {}} />);
    const notes = screen.getByTestId('data-source-v2-notes');
    expect(notes).toBeTruthy();
    for (const text of V2_LIMITATIONS) {
      expect(notes.textContent).toContain(text);
    }
  });

  it('reports the selected value to the caller', () => {
    const onChange = vi.fn();
    render(<DataSourceSelector value="v1" onChange={onChange} />);
    fireEvent.change(screen.getByTestId('data-source-select'), { target: { value: 'v2' } });
    expect(onChange).toHaveBeenCalledWith('v2');
  });

  it('is disabled while a run is in flight', () => {
    render(<DataSourceSelector value="v1" onChange={() => {}} disabled />);
    expect(screen.getByTestId('data-source-select').disabled).toBe(true);
  });

  it('renders a custom seed-only label + helper subtext when provided', () => {
    render(
      <DataSourceSelector
        value="v1"
        onChange={() => {}}
        label="Default source for new instruments"
        helper="Seeds new instruments; existing rows win."
      />,
    );
    expect(screen.getByText('Default source for new instruments')).toBeTruthy();
    expect(screen.getByTestId('data-source-helper').textContent).toContain('existing rows win');
  });

  it('scopes all data-testids under a custom testId base (no collision with a page instance)', () => {
    render(<DataSourceSelector value="v1" onChange={() => {}} testId="picker-data-source" />);
    expect(screen.getByTestId('picker-data-source-select')).toBeTruthy();
    expect(screen.getByTestId('picker-data-source-selector')).toBeTruthy();
    // The default ids are NOT present under a custom base.
    expect(screen.queryByTestId('data-source-select')).toBeNull();
  });
});
