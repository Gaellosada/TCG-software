import { Component } from 'react';

/**
 * Generic error boundary — catches render errors in children and displays
 * a recoverable fallback instead of crashing the entire page.
 *
 * Usage:
 *   <ErrorBoundary>
 *     <SomeComponent />
 *   </ErrorBoundary>
 *
 * Or with a custom fallback:
 *   <ErrorBoundary fallback={<p>Oops</p>}>
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary]', error, info?.componentStack);
  }

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback;

      return (
        <div style={{
          padding: '24px',
          margin: '16px',
          borderRadius: '8px',
          border: '1px solid var(--border-primary, #e5e7eb)',
          background: 'var(--bg-surface, #fff)',
          color: 'var(--text-primary, #1f2937)',
        }}>
          <strong style={{ display: 'block', marginBottom: '8px' }}>
            Something went wrong
          </strong>
          <p style={{ margin: '0 0 12px', color: 'var(--text-secondary, #6b7280)', fontSize: '0.875rem' }}>
            {this.state.error.message || 'Unexpected rendering error'}
          </p>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            style={{
              padding: '6px 16px',
              borderRadius: '6px',
              border: '1px solid var(--border-primary, #d1d5db)',
              background: 'var(--bg-primary, #f9fafb)',
              color: 'var(--text-primary, #1f2937)',
              cursor: 'pointer',
              fontSize: '0.8125rem',
            }}
          >
            Retry
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
