import { describe, it, expect } from 'vitest';
import renderMarkdown from './renderMarkdown';

/**
 * Unit tests for renderMarkdown (Issue 24 RCA-2 / RCA-3).
 */

describe('renderMarkdown — existing functionality', () => {
  it('renders headings h1 / h2 / h3', () => {
    expect(renderMarkdown('# Title')).toContain('<h1>');
    expect(renderMarkdown('## Sub')).toContain('<h2>');
    expect(renderMarkdown('### Deep')).toContain('<h3>');
  });

  it('renders bold **text**', () => {
    const html = renderMarkdown('This is **bold** text.');
    expect(html).toContain('<strong>bold</strong>');
  });

  it('renders inline code `code`', () => {
    const html = renderMarkdown('Run `npm test` now.');
    expect(html).toContain('<code>npm test</code>');
  });

  it('renders fenced code blocks', () => {
    const html = renderMarkdown('```python\nprint("hi")\n```');
    expect(html).toContain('<pre>');
    expect(html).toContain('print(&quot;hi&quot;)');
  });

  it('renders unordered list', () => {
    const html = renderMarkdown('- item one\n- item two');
    expect(html).toContain('<ul>');
    expect(html).toContain('<li>item one</li>');
    expect(html).toContain('<li>item two</li>');
  });

  it('renders ordered list', () => {
    const html = renderMarkdown('1. First\n2. Second');
    expect(html).toContain('<ol>');
    expect(html).toContain('<li>First</li>');
  });

  it('returns empty string for falsy input', () => {
    expect(renderMarkdown('')).toBe('');
    expect(renderMarkdown(null)).toBe('');
    expect(renderMarkdown(undefined)).toBe('');
  });
});

describe('renderMarkdown — RCA-2: italic patterns', () => {
  it('renders _italic_ with underscores', () => {
    const html = renderMarkdown('This is _italic_ text.');
    expect(html).toContain('<em>italic</em>');
    expect(html).not.toContain('_italic_');
  });

  it('renders *italic* with single asterisks', () => {
    const html = renderMarkdown('This is *italic* text.');
    expect(html).toContain('<em>italic</em>');
    expect(html).not.toContain('*italic*');
  });

  it('bold ** is not broken by italic * patterns', () => {
    const html = renderMarkdown('**bold** and *italic*');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<em>italic</em>');
  });

  it('adjacent bold and italic do not interfere', () => {
    const html = renderMarkdown('**bold** _italic_');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<em>italic</em>');
  });

  it('italic inside a sentence does not break surrounding text', () => {
    const html = renderMarkdown('The _quick_ brown fox.');
    expect(html).toContain('<em>quick</em>');
    expect(html).toContain('The ');
    expect(html).toContain(' brown fox.');
  });
});

describe('renderMarkdown — RCA-3: link patterns', () => {
  it('renders [text](https://url) as anchor', () => {
    const html = renderMarkdown('[Anthropic](https://anthropic.com)');
    expect(html).toContain('<a href="https://anthropic.com"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('>Anthropic</a>');
  });

  it('renders [text](http://url) as anchor', () => {
    const html = renderMarkdown('[Example](http://example.com)');
    expect(html).toContain('<a href="http://example.com"');
    expect(html).toContain('>Example</a>');
  });

  it('XSS guard: javascript: URL is not rendered as link', () => {
    const html = renderMarkdown('[evil](javascript:alert(1))');
    // Must not contain an href= with javascript:
    expect(html).not.toContain('href=');
    expect(html).not.toContain('javascript:');
    // The link text should still appear as plain text
    expect(html).toContain('evil');
  });

  it('XSS guard: data: URL is blocked', () => {
    const html = renderMarkdown('[payload](data:text/html,<script>)');
    expect(html).not.toContain('href=');
  });

  it('link has target="_blank"', () => {
    const html = renderMarkdown('[Docs](https://docs.anthropic.com)');
    expect(html).toContain('target="_blank"');
  });

  it('link text is HTML-escaped', () => {
    const html = renderMarkdown('[<script>xss</script>](https://safe.com)');
    expect(html).not.toContain('<script>');
  });

  it('renders multiple links in one line', () => {
    const html = renderMarkdown('[A](https://a.com) and [B](https://b.com)');
    expect(html).toContain('href="https://a.com"');
    expect(html).toContain('href="https://b.com"');
  });
});

describe('renderMarkdown — M2 regression: snake_case false-positive fix', () => {
  it('does NOT italicise snake_case identifiers (my_var_name)', () => {
    const html = renderMarkdown('my_var_name');
    expect(html).not.toContain('<em>');
    expect(html).toContain('my_var_name');
  });

  it('does NOT italicise multi-underscore identifiers (value_1_test)', () => {
    const html = renderMarkdown('value_1_test');
    expect(html).not.toContain('<em>');
    expect(html).toContain('value_1_test');
  });

  it('standalone _italic_ still renders as <em>', () => {
    const html = renderMarkdown('_italic_');
    expect(html).toContain('<em>italic</em>');
  });

  it('parenthesized (_italic_) still renders as <em>', () => {
    const html = renderMarkdown('(_italic_)');
    expect(html).toContain('<em>italic</em>');
  });

  it('whitespace-bounded "Hello _world_!" still renders as <em>', () => {
    const html = renderMarkdown('Hello _world_!');
    expect(html).toContain('<em>world</em>');
    expect(html).toContain('Hello ');
  });
});

describe('renderMarkdown — XSS safety', () => {
  it('escapes < and > in plain text', () => {
    const html = renderMarkdown('<script>alert(1)</script>');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('escapes & in plain text', () => {
    const html = renderMarkdown('Tom & Jerry');
    expect(html).toContain('Tom &amp; Jerry');
  });
});
