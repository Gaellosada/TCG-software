// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import AssumptionsPanel from './AssumptionsPanel';

afterEach(cleanup);

describe('<AssumptionsPanel>', () => {
  it('shows empty state when no assumptions', () => {
    render(<AssumptionsPanel assumptions={[]} />);
    expect(screen.getByText(/no assumptions yet/i)).toBeTruthy();
  });

  it('does not show count badge when empty', () => {
    const { container } = render(<AssumptionsPanel assumptions={[]} />);
    // The count badge should not be present
    const counts = container.querySelectorAll('[class*="count"]');
    expect(counts.length).toBe(0);
  });

  it('renders grouped assumptions', () => {
    const assumptions = [
      { field: 'database', value: 'production', source: 'inferred', confidence: 'high', group: 'Environment' },
      { field: 'collection', value: 'trades', source: 'user', confidence: 'high', group: 'Environment' },
      { field: 'date_range', value: '2024-01-01 to 2024-12-31', source: 'default', confidence: 'medium', group: 'Query' },
    ];
    render(<AssumptionsPanel assumptions={assumptions} />);

    // Group headers
    expect(screen.getByText('Environment')).toBeTruthy();
    expect(screen.getByText('Query')).toBeTruthy();

    // Field names
    expect(screen.getByText('database')).toBeTruthy();
    expect(screen.getByText('collection')).toBeTruthy();
    expect(screen.getByText('date_range')).toBeTruthy();

    // Values
    expect(screen.getByText('production')).toBeTruthy();
    expect(screen.getByText('trades')).toBeTruthy();
  });

  it('applies source stripe class per row (Option B — no badge text)', () => {
    // Option B uses a 3 px left-border stripe for source, not a text badge.
    // Verify the stripe CSS class is applied to each row instead.
    const assumptions = [
      { field: 'db', value: 'prod', source: 'inferred', confidence: 'high', group: 'General' },
      { field: 'user_pref', value: 'dark', source: 'user', confidence: 'high', group: 'General' },
    ];
    const { container } = render(<AssumptionsPanel assumptions={assumptions} />);

    // The raw source name text must NOT appear in the DOM (badge removed in Option B)
    expect(screen.queryByText('inferred')).toBeNull();
    expect(screen.queryByText('user')).toBeNull();

    // Each row must have a stripe class matching the source
    const inferredRow = container.querySelector('[class*="stripeInferred"]');
    expect(inferredRow).toBeTruthy();
    const userRow = container.querySelector('[class*="stripeUser"]');
    expect(userRow).toBeTruthy();
  });

  it('shows count badge when assumptions exist', () => {
    const assumptions = [
      { field: 'db', value: 'prod', source: 'default', confidence: 'high', group: 'General' },
      { field: 'col', value: 'x', source: 'default', confidence: 'medium', group: 'General' },
    ];
    render(<AssumptionsPanel assumptions={assumptions} />);
    expect(screen.getByText('2')).toBeTruthy();
  });

  it('renders rationale when provided', () => {
    const assumptions = [
      {
        field: 'engine',
        value: 'mongo',
        source: 'inferred',
        confidence: 'medium',
        group: 'Config',
        rationale: 'Detected from connection string',
      },
    ];
    render(<AssumptionsPanel assumptions={assumptions} />);
    expect(screen.getByText('Detected from connection string')).toBeTruthy();
  });

  it('handles object values by stringifying', () => {
    const assumptions = [
      { field: 'filter', value: { status: 'active' }, source: 'user', confidence: 'high', group: 'Query' },
    ];
    render(<AssumptionsPanel assumptions={assumptions} />);
    expect(screen.getByText('{"status":"active"}')).toBeTruthy();
  });

  it('handles null values', () => {
    const assumptions = [
      { field: 'limit', value: null, source: 'default', confidence: 'low', group: 'Query' },
    ];
    render(<AssumptionsPanel assumptions={assumptions} />);
    expect(screen.getByText('null')).toBeTruthy();
  });

  it('uses "General" as default group when group is missing', () => {
    const assumptions = [
      { field: 'timezone', value: 'UTC', source: 'default', confidence: 'high' },
    ];
    render(<AssumptionsPanel assumptions={assumptions} />);
    expect(screen.getByText('General')).toBeTruthy();
  });
});
