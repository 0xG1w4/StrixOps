export type CredentialStatus = "unverified" | "validated" | "failed" | "unknown";
export type CredentialSourceStatus = "available" | "missing" | "partial" | "unreadable";

export interface Credential {
  id: string;
  host: string;
  username: string;
  password: string;
  hash: string;
  source: string;
  severity: string;
  note: string;
  secret_type: string;
  validation_status: CredentialStatus;
  sources: Array<{ kind: string; id: string; title: string }>;
}

export interface CredentialsPage {
  credentials: Credential[];
  source_status: CredentialSourceStatus;
  warnings: string[];
}

export const CREDENTIAL_STATUSES: CredentialStatus[] = ["unverified", "validated", "failed", "unknown"];
const STRING_FIELDS = ["id", "host", "username", "password", "hash", "source", "severity", "note", "secret_type"] as const;

/** Invalid source responses must never look like a successful empty inventory. */
export function parseCredentialsPage(value: unknown): CredentialsPage {
  if (!value || typeof value !== "object") throw new Error("invalid_credentials");
  const data = value as Record<string, unknown>;
  if (!Array.isArray(data.credentials)
    || !["available", "missing", "partial", "unreadable"].includes(String(data.source_status))
    || !Array.isArray(data.warnings) || data.warnings.some(warning => typeof warning !== "string")) {
    throw new Error("invalid_credentials");
  }
  const credentials = data.credentials.map((value: unknown) => {
    if (!value || typeof value !== "object") throw new Error("invalid_credentials");
    const row = value as Record<string, unknown>;
    if (STRING_FIELDS.some(field => typeof row[field] !== "string") || !row.id
      || (row.validation_status != null && !CREDENTIAL_STATUSES.includes(row.validation_status as CredentialStatus))
      || !Array.isArray(row.sources) || row.sources.some(source => !source || typeof source !== "object"
        || ["kind", "id", "title"].some(field => typeof (source as Record<string, unknown>)[field] !== "string"))) {
      throw new Error("invalid_credentials");
    }
    return { ...row, validation_status: row.validation_status ?? "unverified" } as unknown as Credential;
  });
  return { credentials, source_status: data.source_status as CredentialSourceStatus, warnings: data.warnings as string[] };
}

export function filterCredentials(rows: Credential[], search: string, status: CredentialStatus | ""): Credential[] {
  const query = search.trim().toLocaleLowerCase();
  return rows.filter(row => (!status || row.validation_status === status)
    && (!query || [row.host, row.username, row.password, row.hash, row.source, row.note, row.secret_type,
      ...row.sources.flatMap(source => [source.kind, source.id, source.title])]
      .some(value => value.toLocaleLowerCase().includes(query))));
}
