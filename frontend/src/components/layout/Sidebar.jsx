import { NavLink } from 'react-router-dom';
import styles from './Sidebar.module.css';

const NAV_ITEMS = [
  { to: '/help', label: 'Help' },
  { to: '/data', label: 'Data' },
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/research', label: 'Research' },
  { to: '/saved-strategies', label: 'Saved Strategies' },
];

function Sidebar() {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.logo}>
        <span className={styles.logoText}>TCG</span>
      </div>
      <nav className={styles.nav}>
        <ul className={styles.navList}>
          {NAV_ITEMS.map(({ to, label }) => (
            <li key={to} className={styles.navItem}>
              <NavLink
                to={to}
                className={({ isActive }) =>
                  `${styles.navLink} ${isActive ? styles.active : ''}`
                }
              >
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}

export default Sidebar;
