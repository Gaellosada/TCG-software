import styles from './PlaceholderPage.module.css';

// eslint-disable-next-line react/prop-types
function PlaceholderPage({ title, description }) {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>{title}</h1>
      <p className={styles.description}>
        {description ?? 'This page is incoming work. Check back soon.'}
      </p>
    </div>
  );
}

export default PlaceholderPage;
