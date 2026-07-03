import { useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/**
 * Render an overlay into ``document.body`` (a React portal), positioned
 * ``fixed`` relative to an anchor element's viewport rect. This makes custom
 * dropdown/parameter overlays behave like a native ``<select>`` popup: they
 * escape the scrollable editor panel's ``overflow: auto`` clip and any ancestor
 * stacking context, so they always paint in front. Fixes the clipped indicator
 * parameter overlay (the editor column scrolls; an in-flow absolute child was
 * cut off at the panel edge).
 *
 * The overlay re-positions on scroll (capture, so inner scrolls count) and
 * resize, staying attached to its anchor rather than closing. The rendered node
 * is exposed via ``contentRef`` so a parent's click-outside handler can treat
 * clicks inside the portaled node as "inside".
 *
 * Props:
 *   anchorRef    {React.Ref}  the trigger/wrapper element to anchor to.
 *   align        {'left'|'right'}  which edge of the anchor to align to.
 *   contentRef   {React.Ref?} forwarded to the portaled container.
 *   className    {string?}
 *   testId       {string?}
 *   children     {ReactNode}
 */
function AnchoredPortal({ anchorRef, align = 'left', contentRef, className, testId, role, children }) {
  const [rect, setRect] = useState(null);
  const localRef = useRef(null);
  const nodeRef = contentRef || localRef;

  useLayoutEffect(() => {
    function place() {
      const el = anchorRef?.current;
      if (!el || typeof el.getBoundingClientRect !== 'function') return;
      const r = el.getBoundingClientRect();
      setRect({
        top: r.bottom + 4,
        left: r.left,
        right: (typeof window !== 'undefined' ? window.innerWidth : 0) - r.right,
      });
    }
    place();
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [anchorRef]);

  if (rect === null) return null;

  const style = { position: 'fixed', top: `${rect.top}px`, zIndex: 1000 };
  if (align === 'right') style.right = `${rect.right}px`;
  else style.left = `${rect.left}px`;

  return createPortal(
    <div ref={nodeRef} className={className} style={style} data-testid={testId} role={role}>
      {children}
    </div>,
    document.body,
  );
}

export default AnchoredPortal;
