// @vitest-environment jsdom
//
// A1-4 (picker half): PortfolioPickerModal lists ONLY pure portfolios
// (kind:"pure" or legacy/no-kind, and never a doc whose legs contain a
// portfolio leg) — depth-1 enforcement #1. Also excludes ``excludeId``.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import PortfolioPickerModal from './PortfolioPickerModal';

vi.mock('../../api/persistence', () => ({
  listPortfolios: vi.fn(),
  describePersistenceError: (e) => (e && e.message) || 'err',
}));

import { listPortfolios } from '../../api/persistence';

const DOCS = [
  { id: 'pure-legacy', name: 'Legacy Pure', legs: [{ type: 'instrument' }] },        // no kind → pure
  { id: 'pure-explicit', name: 'Explicit Pure', kind: 'pure', legs: [{ type: 'instrument' }] },
  { id: 'composed', name: 'A Composed', kind: 'composed', legs: [{ type: 'portfolio', portfolioId: 'x' }] },
  { id: 'stale-kind', name: 'Stale Kind', kind: 'pure', legs: [{ type: 'portfolio', portfolioId: 'y' }] }, // has a portfolio leg → NOT pure despite kind
  { id: 'self', name: 'Self', kind: 'pure', legs: [{ type: 'instrument' }] },
];

describe('PortfolioPickerModal — pure-only filter', () => {
  beforeEach(() => {
    listPortfolios.mockReset();
    listPortfolios.mockResolvedValue(DOCS);
  });

  it('lists only pure/legacy portfolios; hides composed and portfolio-leg docs and the excluded id', async () => {
    render(
      <PortfolioPickerModal isOpen onClose={() => {}} onSelect={() => {}} excludeId="self" />,
    );

    // The two genuinely-pure rows render.
    await waitFor(() => expect(screen.getByTestId('portfolio-picker-row-pure-legacy')).toBeTruthy());
    expect(screen.getByTestId('portfolio-picker-row-pure-explicit')).toBeTruthy();

    // Composed, stale-kind (has a portfolio leg), and the excluded self are hidden.
    expect(screen.queryByTestId('portfolio-picker-row-composed')).toBeNull();
    expect(screen.queryByTestId('portfolio-picker-row-stale-kind')).toBeNull();
    expect(screen.queryByTestId('portfolio-picker-row-self')).toBeNull();
  });

  it('calls onSelect with the chosen doc', async () => {
    const onSelect = vi.fn();
    render(<PortfolioPickerModal isOpen onClose={() => {}} onSelect={onSelect} />);
    const row = await screen.findByTestId('portfolio-picker-row-pure-explicit');
    row.querySelector('button').click();
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'pure-explicit' }),
    );
  });
});
