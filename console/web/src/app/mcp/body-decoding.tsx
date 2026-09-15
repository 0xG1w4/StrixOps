"use client";

import { type McpMessage } from "@/lib/mcp-api";
import { useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

export function BodyDecodingNotice({ message }: { message: McpMessage }) {
  const c = useMcpCopy();
  const errors: Record<string, string> = {
    invalid_base64: c("保存的内容格式无效，无法解码。", "The stored body format is invalid and could not be decoded."),
    unsupported_encoding: c("当前无法解开这种压缩格式，原始内容已保留。", "This compression format could not be decoded. Original content is retained."),
    invalid_compression: c("压缩内容无法解开，可能已损坏或与标头不一致。", "The compressed body is invalid or does not match its encoding header."),
    incomplete_body: c("内容尚未完整接收或已截断；当前只显示可解码的部分。", "The body is incomplete or truncated; only its decodable portion is shown."),
    invalid_charset: c("无法识别内容声明的字符编码，原始数据已保留。", "The declared character encoding is unsupported. Original data is retained."),
    invalid_text: c("内容不符合声明的文本编码；无法完整转成文字。", "The body does not match its text encoding and could not be fully decoded."),
  };
  return <>
    {message.body_decoded && <div className={styles.decodeNote}>{c("已解压", "Decompressed")} {message.body_encoding || ""}{message.body_charset ? ` · ${message.body_charset}` : ""} · {c("此处显示解码内容，原始传输数据保留。", "Showing decoded content; original transfer data is retained.")}</div>}
    {message.body_decode_error && <div className={styles.warningNote}>{errors[message.body_decode_error] || c("内容无法完整解码，原始数据已保留。", "Content could not be fully decoded. Original data is retained.")}</div>}
    {message.body_preview_truncated && !message.truncated && message.body_decode_error !== "incomplete_body" && <div className={styles.warningNote}>{c("解码内容超过预览上限，仅显示前段。", "Decoded content exceeds the preview limit; only its beginning is shown.")}</div>}
  </>;
}
