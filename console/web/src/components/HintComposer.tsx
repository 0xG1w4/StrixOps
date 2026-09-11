"use client";

import * as React from "react";
import { Send } from "lucide-react";
import { Spinner } from "@/components/ui";
import type { RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { editHintDraft, hintDraftKey, hintDraftSnapshot, hintDraftSubscribe, hintEligibility, submitHintDraft } from "./hint-state";
import styles from "./ConversationControls.module.css";

export default function HintComposer({ name, run, selectedAgentId, focusVersion }: {
  name: string; run: RunDetail | null; selectedAgentId: string; focusVersion: number;
}) {
  const { locale } = useI18n();
  const c = (zh: string, en: string) => locale === "en" ? en : zh;
  const target = selectedAgentId || "root";
  const key = hintDraftKey(name, target);
  const draft = React.useSyncExternalStore(hintDraftSubscribe, () => hintDraftSnapshot(key), () => hintDraftSnapshot(key));
  const textarea = React.useRef<HTMLTextAreaElement>(null);
  React.useEffect(() => {
    if (focusVersion) textarea.current?.focus({ preventScroll: true });
  }, [focusVersion, key]);
  const unavailable = hintEligibility(run, target);
  const sending = draft.attempt?.status === "sending";
  const uncertain = draft.attempt?.status === "uncertain";
  const messageLength = Array.from(draft.text.trim()).length;
  const send = async (retry = false) => {
    if (sending || (!retry && (unavailable || uncertain || !messageLength || messageLength > 16_000))) return;
    await submitHintDraft(name, target, retry);
  };
  const errors: Record<string, string> = {
    run_not_active: c("任務已停止接收新指令。", "This task no longer accepts new hints."),
    agent_not_active: c("此代理已停止接收指令。", "This agent no longer accepts hints."),
    unknown_agent: c("找不到目標代理，請重新選擇。", "The target agent is unavailable. Select an agent again."),
    idempotency_conflict: c("此請求與既有送出紀錄不一致，未重複送出。", "This request conflicts with an existing send record. No duplicate was sent."),
    message_too_long: c("指令過長，請縮短後再送出。", "The hint is too long. Shorten it before sending."),
    invalid_message: c("請輸入有效指令。", "Enter a valid hint."),
  };
  return (
    <div className={styles.composer} aria-label={c("傳送指令", "Send a hint")}>
      <div className={styles.recipient}>
        <span>{c("送給", "To")}: <strong>{run?.agents?.[target]?.name || target}</strong> <code>{target}</code></span>
        {!selectedAgentId && <span>{c("全部對話檢視 · 僅送主代理", "All conversations · sends only to root")}</span>}
      </div>
      <div className={styles.composeRow}>
        <textarea
          ref={textarea}
          value={draft.text}
          onChange={event => editHintDraft(key, event.target.value)}
          onKeyDown={event => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
          placeholder={c("補充測試方向或提供線索…", "Add direction or context for this agent…")}
          aria-label={c("指令內容", "Hint message")}
          disabled={sending}
          readOnly={Boolean(unavailable)}
          rows={2}
        />
        <button type="button" className="button-primary" disabled={Boolean(unavailable) || sending || uncertain || !messageLength || messageLength > 16_000} onClick={() => void send()}>
          {sending ? <Spinner /> : <Send size={15} />}{sending ? c("送出中", "Sending") : c("送出", "Send")}
        </button>
      </div>
      <div className={styles.composerHint}>
        <span>{unavailable === "run_closed" ? c("任務未在執行，無法送出新指令。", "The task is not active. New hints are disabled.")
          : unavailable === "agent_closed" ? c("此代理已結束或正在結束，無法送出新指令。", "This agent has finished or is finishing. New hints are disabled.")
            : unavailable ? c("代理狀態尚未確認，暫時無法送出。", "The agent's status is unknown. Sending is disabled.")
              : c("Ctrl／⌘ + Enter 送出；Enter 換行。", "Ctrl/⌘ + Enter sends; Enter adds a line.")}</span>
        {messageLength > 16_000 && <span className={styles.failure}>{c("上限 16,000 字元", "16,000-character limit")}</span>}
      </div>
      {uncertain && (
        <div className={styles.sendNotice} role="status">
          <span>{c("送出結果尚未確認；確認時會使用原指令與同一請求編號。", "Delivery is unconfirmed. Checking reuses the original hint and request ID.")}</span>
          <button type="button" onClick={() => void send(true)}>{c("確認送出結果", "Check delivery result")}</button>
        </div>
      )}
      {draft.attempt?.status === "rejected" && <p className={styles.failure} role="status">{errors[draft.attempt.code || ""] || c("指令未被接受，請檢查代理狀態後重試。", "The hint was not accepted. Check the agent's status and retry.")}</p>}
      {draft.notice && <p className={styles.composerHint} role="status">{draft.notice === "accepted"
        ? c("指令已受理，交付狀態會顯示在對話時間軸。", "Hint accepted. Delivery status appears in the conversation timeline.")
        : c("指令交付失敗，請查看時間軸原因。", "Hint delivery failed. Review the reason in the timeline.")}</p>}
    </div>
  );
}
