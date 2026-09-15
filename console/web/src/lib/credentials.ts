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
  validation_evidence?: string;
  sources: Array<{ kind: string; id: string; title: string }>;
}

export interface CredentialsPage {
  credentials: Credential[];
  source_status: CredentialSourceStatus;
  warnings: string[];
  total: number;
  overall_total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  summary: {
    validation_status: Record<CredentialStatus, number>;
    secret_type: Record<string, number>;
  };
}

export const CREDENTIAL_STATUSES: CredentialStatus[] = ["unverified", "validated", "failed", "unknown"];
const STRING_FIELDS = ["id", "host", "username", "password", "hash", "source", "severity", "note", "secret_type"] as const;
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/** Invalid source responses must never look like a successful empty inventory. */
export function parseCredentialsPage(value: unknown): CredentialsPage {
  if (!value || typeof value !== "object") throw new Error("invalid_credentials");
  const data = value as Record<string, unknown>;
  if (!Array.isArray(data.credentials)
    || !["available", "missing", "partial", "unreadable"].includes(String(data.source_status))
    || !Array.isArray(data.warnings) || data.warnings.some(warning => typeof warning !== "string")) {
    throw new Error("invalid_credentials");
  }
  const summary = data.summary as CredentialsPage["summary"] | undefined;
  if (!count(data.total) || !count(data.overall_total) || data.total > data.overall_total
    || !count(data.limit) || data.limit < 1 || data.limit > 100
    || !count(data.offset) || typeof data.has_more !== "boolean"
    || data.credentials.length > data.limit || data.credentials.length > data.total
    || !summary || typeof summary !== "object"
    || !summary.validation_status || typeof summary.validation_status !== "object"
    || Array.isArray(summary.validation_status) || Object.values(summary.validation_status).some(value => !count(value))
    || !summary.secret_type || typeof summary.secret_type !== "object" || Array.isArray(summary.secret_type)
    || Object.values(summary.secret_type).some(value => !count(value))) {
    throw new Error("invalid_credentials");
  }
  const credentials = data.credentials.map((value: unknown) => {
    if (!value || typeof value !== "object") throw new Error("invalid_credentials");
    const row = value as Record<string, unknown>;
    if (STRING_FIELDS.some(field => typeof row[field] !== "string") || !row.id
      || (row.validation_evidence !== undefined && typeof row.validation_evidence !== "string")
      || (row.validation_status != null && !CREDENTIAL_STATUSES.includes(row.validation_status as CredentialStatus))
      || !Array.isArray(row.sources) || row.sources.some(source => !source || typeof source !== "object"
        || ["kind", "id", "title"].some(field => typeof (source as Record<string, unknown>)[field] !== "string"))) {
      throw new Error("invalid_credentials");
    }
    return { ...row, validation_status: row.validation_status ?? "unverified" } as unknown as Credential;
  });
  return {
    credentials, source_status: data.source_status as CredentialSourceStatus, warnings: data.warnings as string[],
    total: data.total, overall_total: data.overall_total, limit: data.limit, offset: data.offset,
    has_more: data.has_more,
    summary: {
      validation_status: Object.fromEntries(CREDENTIAL_STATUSES.map(status => [status, summary.validation_status[status] ?? 0])) as Record<CredentialStatus, number>,
      secret_type: { ...summary.secret_type },
    },
  };
}
