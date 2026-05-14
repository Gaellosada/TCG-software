// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import PlaceholderPage from './PlaceholderPage';

afterEach(() => {
  cleanup();
});

describe('<PlaceholderPage>', () => {
  it('renders the title prop', () => {
    render(<PlaceholderPage title="Some Title" />);
    expect(screen.getByText('Some Title')).toBeTruthy();
  });

  it('renders the default description when none is provided', () => {
    render(<PlaceholderPage title="X" />);
    expect(
      screen.getByText(/this page is incoming work/i)
    ).toBeTruthy();
  });

  it('renders a custom description when provided', () => {
    render(
      <PlaceholderPage title="X" description="Custom copy here." />
    );
    expect(screen.getByText('Custom copy here.')).toBeTruthy();
    expect(screen.queryByText(/incoming work/i)).toBeNull();
  });
});
