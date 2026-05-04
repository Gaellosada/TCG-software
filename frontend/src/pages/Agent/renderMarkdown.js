/**
 * Minimal markdown-to-HTML renderer for agent chat and notebook cells.
 *
 * Handles:
 *  - Fenced code blocks (```) with optional language annotation
 *  - Inline code (`)
 *  - Bold (**)
 *  - Line breaks (\n → <br>)
 *
 * Returns an HTML string intended for use with dangerouslySetInnerHTML.
 * Content is escaped before processing to prevent XSS from user/agent text.
 */

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export default function renderMarkdown(text) {
  if (!text) return '';

  // Split on fenced code blocks first — they take priority and their
  // contents must not be processed for inline formatting.
  const parts = text.split(/(```[\s\S]*?```)/g);

  const html = parts
    .map((part) => {
      // Fenced code block
      const fenceMatch = part.match(/^```(\w*)\n?([\s\S]*?)```$/);
      if (fenceMatch) {
        const lang = fenceMatch[1];
        const code = escapeHtml(fenceMatch[2].replace(/\n$/, ''));
        const cls = lang ? ` class="language-${escapeHtml(lang)}"` : '';
        return `<pre><code${cls}>${code}</code></pre>`;
      }

      // Normal text — apply inline formatting
      let escaped = escapeHtml(part);

      // Inline code (single backtick, non-greedy)
      escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

      // Bold
      escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

      // Line breaks
      escaped = escaped.replace(/\n/g, '<br>');

      return escaped;
    })
    .join('');

  return html;
}
