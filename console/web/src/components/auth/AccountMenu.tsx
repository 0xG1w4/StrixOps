"use client";

import { useEffect, useRef, useState } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import * as Dialog from "@radix-ui/react-dialog";
import { Clock3, LoaderCircle, LogOut, RefreshCw, Settings2, X } from "lucide-react";
import { getAccount, logout, useAuth, type AuthAccount } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import PasswordForm from "./PasswordForm";
import StrixAvatar from "./StrixAvatar";
import styles from "./auth.module.css";

function LoginHistory() {
  const { t, locale } = useI18n();
  const [account, setAccount] = useState<AuthAccount | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    void getAccount(controller.signal).then(value => { if (!controller.signal.aborted) setAccount(value); }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [attempt]);
  function date(value: string) {
    const stamp = new Date(value);
    return Number.isFinite(stamp.getTime()) ? stamp.toLocaleString(locale, { dateStyle: "medium", timeStyle: "medium" }) : t("auth.unknownTime");
  }
  return (
    <section className={styles.history} aria-labelledby="login-history-title">
      <h3 id="login-history-title"><Clock3 size={16} />{t("auth.recentLogins")}</h3>
      <p className={styles.hint}>{t("auth.recentLoginsHint")}</p>
      {failed ? <div className={styles.historyError}><p role="status">{t("auth.historyError")}</p><button className="button-secondary" type="button" onClick={() => setAttempt(attempt + 1)}><RefreshCw size={14} />{t("auth.retry")}</button></div>
        : !account ? <p role="status" className={styles.loading}><LoaderCircle size={16} className={styles.spin} />{t("auth.loadingHistory")}</p>
          : !account.login_history.length ? <p className={styles.hint}>{t("auth.noLogins")}</p>
            : <ol className={styles.historyList}>{account.login_history.map((entry, index) => <li key={`${entry.at}-${index}`}><time dateTime={Number.isFinite(Date.parse(entry.at)) ? entry.at : undefined}>{date(entry.at)}</time><code>{entry.ip || t("auth.unknownIP")}</code></li>)}</ol>}
    </section>
  );
}

export default function AccountMenu() {
  const { t } = useI18n();
  const auth = useAuth();
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverOpened = useRef(false);
  const cancelHover = () => { if (hoverTimer.current) clearTimeout(hoverTimer.current); hoverTimer.current = null; };
  useEffect(() => () => cancelHover(), []);
  if (auth.status !== "authenticated") return null;
  function enter(event: React.PointerEvent) {
    if (event.pointerType !== "mouse" || settings) return;
    cancelHover();
    hoverTimer.current = setTimeout(() => { hoverOpened.current = true; setOpen(true); }, 120);
  }
  function leave(event: React.PointerEvent) {
    if (event.pointerType !== "mouse") return;
    cancelHover();
    if (hoverOpened.current) hoverTimer.current = setTimeout(() => setOpen(false), 180);
  }
  async function signOut() {
    if (busy) return;
    setBusy(true); setFailed(false);
    try { await logout(); } catch { setFailed(true); setBusy(false); }
  }
  return (
    <>
      <Menu.Root open={open} onOpenChange={value => { cancelHover(); setOpen(value); }} modal={false}>
        <Menu.Trigger asChild>
          <button ref={trigger} type="button" className={styles.accountTrigger} aria-label={t("auth.accountMenu")} title={t("auth.accountMenu")} onPointerEnter={enter} onPointerLeave={leave} onPointerDown={() => { hoverOpened.current = false; }} onKeyDown={() => { hoverOpened.current = false; }}>
            <StrixAvatar />
          </button>
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content className={styles.menu} align="end" sideOffset={8} collisionPadding={12} onPointerEnter={cancelHover} onPointerLeave={leave} onCloseAutoFocus={event => { if (hoverOpened.current || settings) event.preventDefault(); }}>
            <Menu.Label className={styles.menuIdentity}><StrixAvatar /><span><strong>strix</strong><span>{t("auth.localAccount")}</span></span></Menu.Label>
            <Menu.Separator className={styles.separator} />
            <Menu.Item className={styles.menuItem} onSelect={() => { setOpen(false); setSettings(true); }}><Settings2 size={16} />{t("auth.accountSettings")}</Menu.Item>
            <Menu.Item className={styles.menuItem} disabled={busy} onSelect={event => { event.preventDefault(); void signOut(); }}><LogOut size={16} />{t(busy ? "auth.signingOut" : "auth.signOut")}</Menu.Item>
            {failed && <p role="alert" className={styles.menuError}>{t("auth.logoutError")}</p>}
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
      <Dialog.Root open={settings} onOpenChange={setSettings}>
        <Dialog.Portal>
          <Dialog.Overlay className={styles.overlay} />
          <Dialog.Content className={styles.dialog} onCloseAutoFocus={event => { event.preventDefault(); trigger.current?.focus({ preventScroll: true }); }}>
            <header className={styles.dialogHeader}>
              <div className={styles.dialogIdentity}><StrixAvatar /><div><Dialog.Title>{t("auth.accountSettings")}</Dialog.Title><Dialog.Description>{t("auth.accountDescription")}</Dialog.Description></div></div>
              <Dialog.Close asChild><button type="button" className={styles.closeButton} aria-label={t("auth.closeSettings")}><X size={19} /></button></Dialog.Close>
            </header>
            <div className={styles.dialogBody}>
              <section aria-labelledby="change-password-title"><h3 id="change-password-title" className={styles.sectionTitle}>{t("auth.changePassword")}</h3><PasswordForm /></section>
              <LoginHistory />
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
