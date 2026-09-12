import styles from "./auth.module.css";

/** Original geometric Strix mark; no external image or tracking request. */
export default function StrixAvatar({ large = false }: { large?: boolean }) {
  return (
    <svg viewBox="0 0 64 64" className={large ? styles.avatarLarge : styles.avatar} fill="none" aria-hidden="true" focusable="false">
      <path d="M9 10 24 17H40L55 10 51 32C50 47 41 55 32 59 23 55 14 47 13 32L9 10Z" fill="currentColor" fillOpacity=".10" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
      <path d="M13 23 23 19 32 25 41 19 51 23 47 38 36 40 32 35 28 40 17 38Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx="23" cy="29" r="6" fill="currentColor" fillOpacity=".16" stroke="currentColor" />
      <circle cx="41" cy="29" r="6" fill="currentColor" fillOpacity=".16" stroke="currentColor" />
      <path d="M23 26V32M41 26V32" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
      <path d="M28 36 32 42 36 36M26 47 32 51 38 47" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}
