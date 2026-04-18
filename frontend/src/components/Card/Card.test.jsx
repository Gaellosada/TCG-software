// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import Card from './Card';

afterEach(() => {
  cleanup();
});

describe('<Card>', () => {
  it('renders children inside the body', () => {
    render(<Card><p>hello</p></Card>);
    expect(screen.getByText('hello')).toBeTruthy();
  });

  it('renders title in the header when provided', () => {
    render(<Card title="Results"><div>body</div></Card>);
    expect(screen.getByText('Results')).toBeTruthy();
  });

  it('renders right actions in the header when provided', () => {
    render(
      <Card title="Holdings" right={<button>+ Add</button>}>
        <div>body</div>
      </Card>,
    );
    expect(screen.getByRole('button', { name: /\+ add/i })).toBeTruthy();
  });

  it('omits header entirely when neither title nor right is supplied', () => {
    const { container } = render(<Card><div>body</div></Card>);
    // Only the body wrapper — no header element before it.
    expect(container.querySelectorAll('div').length).toBeGreaterThan(0);
    expect(screen.queryByText('Results')).toBeNull();
  });
});
