/** A run-relative identity joins the two public indexes without widening file access. */
export type FileFilter = "all" | "evidence" | "artifacts";
export interface EvidenceMetadata {
  category: string;
  categoryLabel: string;
  sha256: string;
  collectedAt: string;
  oversize: boolean;
  deliverable: boolean;
  error: string;
}
export interface RunFile {
  path: string;
  size: number;
  mtime?: number;
  evidence: boolean;
  metadata?: EvidenceMetadata;
  downloadPath: string | null;
}
const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const text = (value: unknown) => typeof value === "string" ? value : "";
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;

export function canonicalRunPath(value: unknown): string | null {
  if (typeof value !== "string" || !value || value.startsWith("/") || value.includes("\0")) return null;
  const parts = value.split("/").filter(part => part && part !== ".");
  if (!parts.length || parts.some(part => part === "..")) return null;
  return parts.join("/");
}
export function artifactPath(name: string, path: string): string {
  return `/api/runs/${encodeURIComponent(name)}/artifacts/${path.split("/").map(encodeURIComponent).join("/")}`;
}
export function mergeRunFiles(name: string, artifacts: unknown[], evidence: unknown[]): RunFile[] {
  const rows = new Map<string, RunFile>();
  for (const value of artifacts) {
    const row = record(value);
    const path = canonicalRunPath(row?.path);
    if (!row || !path || row.is_dir === true) continue;
    rows.set(path, {
      path, size: number(row.size), mtime: typeof row.mtime === "number" ? row.mtime : undefined,
      evidence: path.startsWith("evidence/"), downloadPath: artifactPath(name, path),
    });
  }
  for (const value of evidence) {
    const row = record(value);
    const filename = canonicalRunPath(row?.filename);
    if (!row || !filename) continue;
    const path = `evidence/${filename}`;
    const previous = rows.get(path);
    // Explicit failed delivery wins even when the other index contains a path.
    const deliverable = typeof row.deliverable === "boolean" ? row.deliverable
      : typeof row.persisted === "boolean" ? row.persisted
      : row.oversize !== true && Boolean(text(row.download_url));
    rows.set(path, {
      path, size: typeof row.size === "number" ? number(row.size) : previous?.size ?? 0,
      mtime: previous?.mtime, evidence: true,
      metadata: {
        category: text(row.category) || "other", categoryLabel: text(row.category_label),
        sha256: text(row.sha256), collectedAt: text(row.collected_at),
        oversize: row.oversize === true, deliverable, error: text(row.error),
      },
      // Never follow a URL from the manifest. The server validates this fixed route again.
      downloadPath: deliverable
        ? `/api/runs/${encodeURIComponent(name)}/evidence/${filename.split("/").map(encodeURIComponent).join("/")}`
        : null,
    });
  }
  return [...rows.values()].sort((a, b) => a.path.localeCompare(b.path));
}
export function filterRunFiles(files: RunFile[], filter: FileFilter, query: string): RunFile[] {
  const q = query.trim().toLocaleLowerCase();
  return files.filter(file =>
    (filter === "all" || (filter === "evidence" ? file.evidence : !file.evidence)) &&
    (!q || [file.path, file.metadata?.category, file.metadata?.categoryLabel, file.metadata?.sha256]
      .some(value => value?.toLocaleLowerCase().includes(q))));
}
export interface FileTree { directories: Map<string, FileTree>; files: RunFile[] }
export function buildFileTree(files: RunFile[]): FileTree {
  const root: FileTree = { directories: new Map(), files: [] };
  for (const file of files) {
    let current = root;
    for (const part of file.path.split("/").slice(0, -1)) {
      if (!current.directories.has(part)) current.directories.set(part, { directories: new Map(), files: [] });
      current = current.directories.get(part)!;
    }
    current.files.push(file);
  }
  return root;
}
export const extension = (path: string) => (path.split("/").pop()?.match(/\.([^.]+)$/)?.[1] || "").toLowerCase();
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export const PREVIEW_BYTES = 400_000;
export interface FilePreview { text: string; binary: boolean; truncated: boolean }
export async function readPreview(response: Response, signal: AbortSignal): Promise<FilePreview> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("preview_unavailable");
  const chunks: Uint8Array[] = [];
  let size = 0;
  let truncated = false;
  try {
    while (true) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const result = await reader.read();
      if (result.done) break;
      const remaining = PREVIEW_BYTES + 1 - size;
      const chunk = result.value.subarray(0, Math.max(0, remaining));
      chunks.push(chunk);
      size += chunk.length;
      if (size > PREVIEW_BYTES) { truncated = true; break; }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  const range = response.headers.get("content-range")?.match(/bytes\s+\d+-\d+\/(\d+)/i);
  if (range && Number(range[1]) > size) truncated = true;
  const bytes = new Uint8Array(Math.min(size, PREVIEW_BYTES));
  let offset = 0;
  for (const chunk of chunks) {
    const slice = chunk.subarray(0, bytes.length - offset);
    bytes.set(slice, offset); offset += slice.length;
  }
  const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? "utf-16le"
    : bytes[0] === 0xfe && bytes[1] === 0xff ? "utf-16be" : "utf-8";
  let decoded: string;
  try {
    // stream=true avoids calling a cut UTF-8 character a binary file.
    decoded = new TextDecoder(encoding, { fatal: true }).decode(bytes, { stream: truncated });
  } catch { return { text: "", binary: true, truncated }; }
  const sample = decoded.slice(0, 8192);
  const controls = sample.match(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g)?.length ?? 0;
  if (sample.includes("\0") || controls > Math.max(2, sample.length * 0.01)) return { text: "", binary: true, truncated };
  return { text: decoded, binary: false, truncated };
}

/** Only user-clicked safe external links or already listed run files can be linked. */
export function fileLink(value: string, currentPath: string, files: RunFile[]): string {
  if (!value || /[\x00-\x20\x7f]/.test(value) || value.startsWith("//") || value.includes("\\")) return "";
  if (/^[a-z][a-z\d+.-]*:/i.test(value)) {
    try { const url = new URL(value); return ["https:", "http:", "mailto:"].includes(url.protocol) ? value : ""; }
    catch { return ""; }
  }
  if (value.startsWith("#")) return value;
  if (value.startsWith("/")) return "";
  const parts = currentPath.split("/").slice(0, -1);
  try {
    for (const raw of value.split(/[?#]/, 1)[0].split("/")) {
      const part = decodeURIComponent(raw);
      if (part === "..") { if (!parts.length) return ""; parts.pop(); }
      else if (part && part !== ".") { if (part.includes("/") || part.includes("\0")) return ""; parts.push(part); }
    }
  } catch { return ""; }
  return files.find(file => file.path === parts.join("/"))?.downloadPath ?? "";
}

export function parseFileCsv(text: string): string[][] {
  const rows: string[][] = []; let row: string[] = []; let cell = ""; let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') { row.push(cell); cell = ""; }
    else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ""; }
    else if (ch !== '\r') cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
