import styles from './Card.module.css';

/**
 * Shared titled card / section panel.
 *
 * Pattern mirrors Portfolio's `.section` style: a surface-coloured panel
 * with a thin border, rounded corners, and an optional header row
 * containing a title on the left and arbitrary actions on the right.
 *
 * Props:
 *   title          {ReactNode=}  shown on the left of the header
 *   right          {ReactNode=}  shown on the right of the header (actions)
 *   className      {string=}     extra class on the root
 *   bodyClassName  {string=}     extra class on the body wrapper
 *   children       {ReactNode}   card body
 *
 * The header is only rendered if either `title` or `right` is provided.
 */
function Card({ title, right, className, bodyClassName, children, ...rest }) {
  const rootClass = className ? `${styles.root} ${className}` : styles.root;
  const bodyClass = bodyClassName ? `${styles.body} ${bodyClassName}` : styles.body;
  const hasHeader = title !== undefined || right !== undefined;
  return (
    <div className={rootClass} {...rest}>
      {hasHeader && (
        <div className={styles.header}>
          {title !== undefined && <span className={styles.title}>{title}</span>}
          {right !== undefined && <span className={styles.actions}>{right}</span>}
        </div>
      )}
      <div className={bodyClass}>{children}</div>
    </div>
  );
}

export default Card;
