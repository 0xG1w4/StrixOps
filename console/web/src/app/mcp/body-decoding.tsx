"use client";

import { type McpMessage } from "@/lib/mcp-api";
import { useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

export function BodyDecodingNotice({ message }: { message: McpMessage }) {
  const c = useMcpCopy();
  const errors: Record<string, string> = {
    invalid_base64: c("保存的內容格式無效，無法解碼。", "The stored body format is invalid and could not be decoded."),
    unsupported_encoding: c("目前無法解開這種壓縮格式，原始內容已保留。", "This compression format could not be decoded. Original content is retained."),
    invalid_compression: c("壓縮內容無法解開，可能已損壞或與標頭不一致。", "The compressed body is invalid or does not match its encoding header."),
    incomplete_body: c("內容尚未完整接收或已截斷；目前只顯示可解碼的部分。", "The body is incomplete or truncated; only its decodable portion is shown."),
    invalid_charset: c("無法識別內容宣告的字元編碼，原始資料已保留。", "The declared character encoding is unsupported. Original data is retained."),
    invalid_text: c("內容不符合宣告的文字編碼；無法完整轉成文字。", "The body does not match its text encoding and could not be fully decoded."),
  };
  return <>
    {message.body_decoded && <div className={styles.decodeNote}>{c("已解壓", "Decompressed")} {message.body_encoding || ""}{message.body_charset ? ` · ${message.body_charset}` : ""} · {c("此處顯示解碼內容，原始傳輸資料保留。", "Showing decoded content; original transfer data is retained.")}</div>}
    {message.body_decode_error && <div className={styles.warningNote}>{errors[message.body_decode_error] || c("內容無法完整解碼，原始資料已保留。", "Content could not be fully decoded. Original data is retained.")}</div>}
    {message.body_preview_truncated && !message.truncated && message.body_decode_error !== "incomplete_body" && <div className={styles.warningNote}>{c("解碼內容超過預覽上限，僅顯示前段。", "Decoded content exceeds the preview limit; only its beginning is shown.")}</div>}
  </>;
}
