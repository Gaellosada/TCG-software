import { NavLink } from 'react-router-dom';
import Icon from '../Icon';
import { NAV_SECTIONS } from './navConfig';
import styles from './Sidebar.module.css';

function Sidebar({ collapsed, onToggle }) {
  // First anchor:'bottom' section gets margin-top:auto; later bottom-anchored sections stack via flex.
  const firstBottomIdx = NAV_SECTIONS.findIndex((s) => s.anchor === 'bottom');

  return (
    <aside className={`${styles.sidebar} ${collapsed ? styles.collapsed : ''}`}>
      <div className={styles.logo}>
        {!collapsed && <span className={styles.logoText}>TCG</span>}
        <button
          className={styles.toggle}
          onClick={onToggle}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!collapsed}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <Icon name={collapsed ? 'chevron-right' : 'chevron-left'} size={16} />
        </button>
      </div>
      {NAV_SECTIONS.map((section, idx) => {
        const isFirstBottom = idx === firstBottomIdx;
        const sectionClass = [
          styles.section,
          isFirstBottom ? styles.sectionBottom : '',
        ]
          .filter(Boolean)
          .join(' ');
        return (
          <div
            key={section.id}
            className={sectionClass}
            data-section-id={section.id}
          >
            {idx > 0 && <div className={styles.sectionDivider} />}
            {!collapsed && (
              <span className={styles.sectionLabel}>{section.label}</span>
            )}
            <nav>
              <ul className={styles.navList}>
                {section.items.map(({ path, label, icon }) => (
                  <li key={path} className={styles.navItem}>
                    <NavLink
                      to={path}
                      className={({ isActive }) =>
                        `${styles.navLink} ${isActive ? styles.active : ''}`
                      }
                      title={collapsed ? label : undefined}
                    >
                      <span className={styles.navIcon}>
                        <Icon name={icon} size={18} />
                      </span>
                      {!collapsed && (
                        <span className={styles.navLabel}>{label}</span>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </nav>
          </div>
        );
      })}
    </aside>
  );
}

export default Sidebar;
