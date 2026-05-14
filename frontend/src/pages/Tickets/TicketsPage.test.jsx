// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import TicketsPage from './TicketsPage';

afterEach(() => {
  cleanup();
});

describe('<TicketsPage>', () => {
  it('mounts without runtime error', () => {
    expect(() => render(<TicketsPage />)).not.toThrow();
  });

  it('renders the title "Tickets"', () => {
    render(<TicketsPage />);
    expect(screen.getByText('Tickets')).toBeTruthy();
  });
});
