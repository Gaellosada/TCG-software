// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import RunningSignalsPage from './RunningSignalsPage';

afterEach(() => {
  cleanup();
});

describe('<RunningSignalsPage>', () => {
  it('mounts without runtime error', () => {
    expect(() => render(<RunningSignalsPage />)).not.toThrow();
  });

  it('renders the title "Running Signals"', () => {
    render(<RunningSignalsPage />);
    expect(screen.getByText('Running Signals')).toBeTruthy();
  });
});
