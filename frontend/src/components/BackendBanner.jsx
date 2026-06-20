import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { HEALTH_URL, isTauri } from '../api/base';
import styles from './BackendBanner.module.css';

const POLL_MS = 4000;

// App-wide affordance shown ONLY under Tauri when the auto-spawned backend is
// unreachable (e.g. first run with no credentials, or a bad password). It tells
// the user to set credentials in Settings and links there. It never blocks the
// UI — it is a thin bar above the content — so the Settings page itself stays
// usable while the backend is down. In web mode isTauri() is false and this
// renders nothing.
function BackendBanner() {
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    if (!isTauri()) return undefined;

    let cancelled = false;
    let timer = null;

    async function check() {
      let ok = false;
      try {
        const res = await fetch(HEALTH_URL, { cache: 'no-store' });
        ok = res.ok;
      } catch {
        ok = false;
      }
      if (cancelled) return;
      setReachable(ok);
      timer = setTimeout(check, POLL_MS);
    }

    check();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  if (!isTauri() || reachable) return null;

  return (
    <div className={styles.banner} role="alert" data-testid="backend-banner">
      <span>
        Backend not connected — set your database credentials in{' '}
        <Link to="/settings" className={styles.link}>
          Settings
        </Link>
        .
      </span>
    </div>
  );
}

export default BackendBanner;
