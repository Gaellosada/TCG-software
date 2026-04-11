import { NavLink } from 'react-router-dom';
import Icon from '../Icon';
import styles from './Sidebar.module.css';

const MAIN_NAV = [
  { to: '/data', label: 'Data', icon: 'data' },
  { to: '/portfolio', label: 'Portfolio', icon: 'portfolio' },
  { to: '/research', label: 'Research', icon: 'research' },
];

const BOTTOM_NAV = [
  { to: '/help', label: 'Help', icon: 'help' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
];

function Sidebar({ collapsed, onToggle }) {
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
      <nav className={styles.topNav}>
        <ul className={styles.navList}>
          {MAIN_NAV.map(({ to, label, icon }) => (
            <li key={to} className={styles.navItem}>
              <NavLink
                to={to}
                className={({ isActive }) =>
                  `${styles.navLink} ${isActive ? styles.active : ''}`
                }
                title={collapsed ? label : undefined}
              >
                <span className={styles.navIcon}><Icon name={icon} size={18} /></span>
                {!collapsed && <span className={styles.navLabel}>{label}</span>}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className={styles.spacer} />
      <div className={styles.divider} />
      <nav className={styles.bottomNav}>
        <ul className={styles.navList}>
          {BOTTOM_NAV.map(({ to, label, icon }) => (
            <li key={to} className={styles.navItem}>
              <NavLink
                to={to}
                className={({ isActive }) =>
                  `${styles.navLink} ${isActive ? styles.active : ''}`
                }
                title={collapsed ? label : undefined}
              >
                <span className={styles.navIcon}><Icon name={icon} size={18} /></span>
                {!collapsed && <span className={styles.navLabel}>{label}</span>}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}

export default Sidebar;
