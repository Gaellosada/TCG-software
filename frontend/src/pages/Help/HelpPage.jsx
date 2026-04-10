import styles from './HelpPage.module.css';

function HelpPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Help</h1>
      <p className={styles.description}>
        Documentation and guides for the TCG simulation platform.
      </p>
      <div className={styles.placeholder}>
        <p>Content loading...</p>
      </div>
    </div>
  );
}

export default HelpPage;
