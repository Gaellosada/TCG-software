/**
 * Minimal markdown-to-HTML renderer for agent chat and notebook cells.
 *
 * Handles:
 *  - Fenced code blocks (```) with optional language annotation
 *  - Inline code (`)
 *  - Bold (**)
 *  - Headings: # h1, ## h2, ### h3
 *  - Unordered lists: - item / * item
 *  - Ordered lists: 1. item
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

/**
 * Apply inline formatting (inline code, bold) to a raw (unescaped) string.
 * Escapes HTML first, then applies patterns.
 */
function applyInline(raw) {
  let s = escapeHtml(raw);
  // Inline code (single backtick, non-greedy)
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return s;
}

export default function renderMarkdown(text) {
  if (!text) return '';
  // Guard: content may be an array of content blocks (Anthropic API format)
  // when restored from conversation history.
  if (typeof text !== 'string') {
    if (Array.isArray(text)) {
      const textParts = text
        .filter((b) => b && b.type === 'text')
        .map((b) => b.text || '');
      return renderMarkdown(textParts.join(''));
    }
    return '';
  }

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

      // Normal text — process line-by-line for block-level constructs
      // (headings, lists), then apply inline formatting within each line.
      const lines = part.split('\n');
      const outputParts = [];
      let i = 0;
      while (i < lines.length) {
        const line = lines[i];

        // Headings: check ### before ## before # to avoid short-circuit errors
        const h3 = line.match(/^### (.+)$/);
        const h2 = !h3 && line.match(/^## (.+)$/);
        const h1 = !h3 && !h2 && line.match(/^# (.+)$/);
        if (h3) {
          outputParts.push(`<h3>${applyInline(h3[1])}</h3>`);
          i++;
          continue;
        }
        if (h2) {
          outputParts.push(`<h2>${applyInline(h2[1])}</h2>`);
          i++;
          continue;
        }
        if (h1) {
          outputParts.push(`<h1>${applyInline(h1[1])}</h1>`);
          i++;
          continue;
        }

        // Unordered list: consecutive lines starting with "- " or "* "
        if (/^[-*] /.test(line)) {
          const items = [];
          while (i < lines.length && /^[-*] /.test(lines[i])) {
            items.push(`<li>${applyInline(lines[i].slice(2))}</li>`);
            i++;
          }
          outputParts.push(`<ul>${items.join('')}</ul>`);
          continue;
        }

        // Ordered list: consecutive lines starting with "N. "
        if (/^\d+\. /.test(line)) {
          const items = [];
          while (i < lines.length && /^\d+\. /.test(lines[i])) {
            const content = lines[i].replace(/^\d+\. /, '');
            items.push(`<li>${applyInline(content)}</li>`);
            i++;
          }
          outputParts.push(`<ol>${items.join('')}</ol>`);
          continue;
        }

        // Plain line — apply inline formatting
        outputParts.push(applyInline(line));
        i++;
      }

      return outputParts.join('<br>');
    })
    .join('');

  return html;
}
