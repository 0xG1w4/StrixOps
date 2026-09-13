"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowRight, LoaderCircle, LogOut, RefreshCw, ShieldCheck } from "lucide-react";
import { AuthError, login, logout, refreshAuthSession, useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import StrixAvatar from "./StrixAvatar";
import PasswordField from "./PasswordField";
import PasswordForm from "./PasswordForm";
import styles from "./auth.module.css";
import scene from "./LoginScene.module.css";

function LoginForm() {
  const { t } = useI18n();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !username || !password) return;
    setBusy(true); setError("");
    try { await login(username, password); }
    catch (problem) {
      if (mounted.current) setError(problem instanceof AuthError && problem.code === "rate_limited" ? "auth.rateLimited" : "auth.loginError");
    } finally { if (mounted.current) setBusy(false); }
  }
  return (
    <form className={`${styles.form} ${scene.form}`} onSubmit={submit}>
      <div className={styles.field}>
        <label htmlFor="strix-username">{t("auth.username")}</label>
        <input
          id="strix-username" type="text" name="username" value={username}
          onChange={event => setUsername(event.target.value)} autoComplete="username"
          autoCapitalize="none" spellCheck={false} required disabled={busy}
        />
      </div>
      <PasswordField label={t("auth.password")} value={password} onChange={setPassword} autoComplete="current-password" disabled={busy} />
      {error && <p className={styles.error} role="alert">{t(error)}</p>}
      <button className={scene.submit} type="submit" disabled={busy || !username || !password}>
        {busy ? <LoaderCircle size={16} className={styles.spin} /> : <ArrowRight size={16} />}
        {t(busy ? "auth.signingIn" : "auth.login")}
      </button>
    </form>
  );
}

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const { t, locale, setLocale } = useI18n();
  const [retrying, setRetrying] = useState(false);
  const [logoutError, setLogoutError] = useState(false);
  useEffect(() => {
    void refreshAuthSession();
    const check = () => { if (document.visibilityState === "visible") void refreshAuthSession(); };
    const timer = window.setInterval(check, 60_000);
    document.addEventListener("visibilitychange", check);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", check); };
  }, []);
  if (auth.status === "authenticated" && !auth.session.must_change_password) return children;
  const forced = auth.status === "authenticated" && auth.session.must_change_password;
  if (!forced) {
    return (
      <main className={scene.scene}>
        <div className={scene.artwork} aria-hidden="true" />
        <section className={scene.panel} aria-labelledby="auth-title">
          <header className={scene.header}>
            <StrixAvatar />
            <h1 id="auth-title">Login</h1>
          </header>
          {auth.status === "anonymous" && <LoginForm />}
          {auth.status === "loading" && (
            <div className={scene.loading} role="status" aria-label={t("auth.checking")}>
              <LoaderCircle size={22} className={styles.spin} aria-hidden="true" />
            </div>
          )}
          {auth.status === "unavailable" && (
            <div className={scene.unavailable}>
              <p className={styles.error} role="alert">{t("auth.connectionError")}</p>
              <button className={scene.submit} type="button" disabled={retrying} onClick={async () => {
                setRetrying(true);
                await refreshAuthSession();
                setRetrying(false);
              }}>
                <RefreshCw size={16} className={retrying ? styles.spin : undefined} />
                {t("auth.retry")}
              </button>
            </div>
          )}
        </section>
      </main>
    );
  }
  return (
    <main className={styles.gate}>
      <div className={styles.gateTools} role="group" aria-label={t("shell.language")}>
        <button type="button" onClick={() => setLocale("zh-CN")} aria-pressed={locale === "zh-CN"}>简</button>
        <button type="button" onClick={() => setLocale("en")} aria-pressed={locale === "en"}>EN</button>
      </div>
      <section className={styles.loginCard} aria-labelledby="auth-title">
        <div className={styles.brand}><StrixAvatar large /><span>STRIXOPS<span>CONSOLE</span></span></div>
        <div className={styles.intro}>
          <span className={styles.eyebrow}><ShieldCheck size={14} />{t("auth.consoleAccess")}</span>
          <h1 id="auth-title">{t("auth.firstChange")}</h1>
          <p>{t("auth.firstChangeHint")}</p>
        </div>
        <>
          <PasswordForm forced />
          <button type="button" className={styles.textButton} onClick={async () => { try { await logout(); } catch { setLogoutError(true); } }}><LogOut size={15} />{t("auth.signOut")}</button>
          {logoutError && <p role="alert" className={styles.error}>{t("auth.logoutError")}</p>}
        </>
      </section>
    </main>
  );
}
