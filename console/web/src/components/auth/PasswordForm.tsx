"use client";

import { useId, useRef, useEffect, useState } from "react";
import { Check, KeyRound, LoaderCircle } from "lucide-react";
import { AuthError, changePassword } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import PasswordField from "./PasswordField";
import styles from "./auth.module.css";

export default function PasswordForm({ forced = false }: { forced?: boolean }) {
  const { t } = useI18n();
  const helpId = useId();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setSaved(false);
    const passwordLength = Array.from(next.normalize("NFKC")).length;
    if (passwordLength < 8 || passwordLength > 32) { setError("auth.passwordLength"); return; }
    if (next !== confirmation) { setError("auth.passwordMismatch"); return; }
    setBusy(true); setError("");
    try {
      await changePassword(current, next);
      if (mounted.current) { setCurrent(""); setNext(""); setConfirmation(""); setSaved(true); }
    } catch (problem) {
      if (mounted.current) setError(problem instanceof AuthError && problem.code === "rate_limited" ? "auth.rateLimited" : "auth.passwordError");
    } finally { if (mounted.current) setBusy(false); }
  }
  return (
    <form className={styles.form} onSubmit={submit}>
      <input className={styles.srOnly} type="text" name="username" autoComplete="username" readOnly value="strix" tabIndex={-1} aria-hidden="true" />
      <PasswordField label={t("auth.currentPassword")} value={current} onChange={setCurrent} autoComplete="current-password" disabled={busy} />
      <PasswordField label={t("auth.newPassword")} value={next} onChange={setNext} autoComplete="new-password" disabled={busy} describedBy={helpId} />
      <p id={helpId} className={styles.hint}>{t("auth.passwordLength")}</p>
      <PasswordField label={t("auth.confirmPassword")} value={confirmation} onChange={setConfirmation} autoComplete="new-password" disabled={busy} />
      {error && <p role="alert" className={styles.error}>{t(error)}</p>}
      {saved && <p role="status" className={styles.success}><Check size={16} />{t("auth.passwordSaved")}</p>}
      <button type="submit" className="button-primary" disabled={busy || !current || !next || !confirmation}>
        {busy ? <LoaderCircle size={16} className={styles.spin} /> : <KeyRound size={16} />}
        {t(busy ? "auth.saving" : forced ? "auth.changeAndContinue" : "auth.changePassword")}
      </button>
    </form>
  );
}
