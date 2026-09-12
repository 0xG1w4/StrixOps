"use client";

import { useId, useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import styles from "./auth.module.css";

export default function PasswordField({ label, value, onChange, autoComplete, disabled = false, describedBy }: {
  label: string; value: string; onChange: (value: string) => void;
  autoComplete: "current-password" | "new-password"; disabled?: boolean; describedBy?: string;
}) {
  const id = useId();
  const [visible, setVisible] = useState(false);
  const { t } = useI18n();
  return (
    <div className={styles.field}>
      <label htmlFor={id}>{label}</label>
      <div className={styles.password}>
        <input id={id} name={autoComplete === "current-password" ? "current_password" : id} type={visible ? "text" : "password"} value={value} onChange={event => onChange(event.target.value)} autoComplete={autoComplete} required disabled={disabled} aria-describedby={describedBy} spellCheck={false} autoCapitalize="none" />
        <button type="button" onClick={() => setVisible(!visible)} aria-label={`${t(visible ? "auth.hidePassword" : "auth.showPassword")}: ${label}`} aria-pressed={visible} disabled={disabled}>
          {visible ? <EyeOff size={17} /> : <Eye size={17} />}
        </button>
      </div>
    </div>
  );
}
