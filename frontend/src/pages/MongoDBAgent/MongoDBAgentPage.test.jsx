// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import MongoDBAgentPage from './MongoDBAgentPage';

afterEach(() => {
  cleanup();
});

describe('<MongoDBAgentPage>', () => {
  it('mounts without runtime error', () => {
    expect(() => render(<MongoDBAgentPage />)).not.toThrow();
  });

  it('renders the title "MongoDB Agent"', () => {
    render(<MongoDBAgentPage />);
    expect(screen.getByText('MongoDB Agent')).toBeTruthy();
  });
});
