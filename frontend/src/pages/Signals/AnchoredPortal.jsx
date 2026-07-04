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
 * resize, staying attached to its anchor rather than closing. It is also
 * clamped to a ``max-height`` bounded by the space below the anchor (with
 * ``overflow-y: auto``), so a tall overlay opened near the viewport bottom
 * scrolls internally instead of running off-screen. The rendered node is
 * exposed via ``contentRef`` so a parent's click-outside handler can treat
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
      const top = r.bottom + 4;
      const vh = typeof window !== 'undefined' ? window.innerHeight : 0;
      // Bound the overlay to the space between its top edge and the viewport
      // bottom (less an 8px margin) so a tall overlay (e.g. an indicator-params
      // override opened from a block near the viewport bottom) SCROLLS inside
      // itself instead of running off-screen. Floor keeps it usable when the
      // anchor sits very low. No upward flip — the clamp alone guarantees the
      // overlay never exceeds the viewport.
      const maxHeight = Math.max(120, vh - top - 8);
      setRect({
        top,
        left: r.left,
        right: (typeof window !== 'undefined' ? window.innerWidth : 0) - r.right,
        maxHeight,
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

  // Layering: this portal is appended to document.body, so it wins ties by DOM
  // order. Keep it BELOW the modal/dialog layer (ConfirmDialog / picker modals
  // all sit at z-index 1000) or a dropdown paints over — and stays interactive
  // above — an open dialog. 900 sits above all in-page content (Signals panels
  // top out at z-index 40; the sidebar at 100) and below every modal.
  const style = {
    position: 'fixed',
    top: `${rect.top}px`,
    zIndex: 900,
    maxHeight: `${rect.maxHeight}px`,
    overflowY: 'auto',
  };
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
