export type Location = {
  statement: number;
  byte_offset: number;
  byte_length: number;
  line: number;
  column: number;
};
export type Finding = {
  rule_id: string;
  rule_version: string;
  severity: string;
  category: string;
  location: Location;
  explanation: string;
  evidence: Record<string, unknown>;
  mitigation: string;
};
export type Analysis = {
  artifact_hash: string;
  decision: "blocked" | "review_required";
  tested: boolean;
  policy_version: string;
  postgres_version: string;
  assumptions: string[];
  parsed: {
    valid: boolean;
    parser_version: string;
    operations: {
      kind: string;
      table?: string;
      column?: string;
      location: Location;
      sql: string;
      supported: boolean;
    }[];
    diagnostics: { message: string; location?: Location }[];
  };
  findings: Finding[];
  score: {
    value: number;
    explanation: string;
    policy_version: string;
    contributions: { category: string; deduction: number; reason: string }[];
  };
};
export type Plan = {
  eligible: boolean;
  reason: string;
  artifact_hash: string;
  plan_hash?: string;
  warnings: string[];
  steps: {
    id: string;
    phase: string;
    description: string;
    sql?: string | null;
    transaction: string;
    requires_approval: boolean;
    preconditions: string[];
    verification: string[];
  }[];
};
export type Report = {
  id: string;
  status: string;
  kind?: string;
  artifact_hash: string;
  checks: unknown;
  metrics: Record<string, unknown>;
  error?: string | null;
  created_at?: string;
};
export type Approval = {
  id: string;
  artifact_hash: string;
  report_id: string;
  reviewer: string;
  consumed?: boolean;
};
export type Migration = {
  id: string;
  title: string;
  sql: string;
  created_at: string;
  analysis: Analysis;
  plan?: Plan | null;
  reports: Report[];
  approvals: Approval[];
};
export type Capabilities = {
  sandbox_available: boolean;
  sandbox_enabled?: boolean;
  reason?: string;
  [key: string]: unknown;
};
export type Audit = {
  id: string;
  event: string;
  created_at: string;
  details?: unknown;
  subject_id?: string;
};
export type Job = {
  id: string;
  migration_id: string;
  status: string;
  kind?: string;
  progress?: Record<string, unknown>;
  error?: string;
  report?: { progress?: Record<string, unknown>; error?: string };
  [key: string]: unknown;
};
export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export async function request<T>(
  path: string,
  token: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(120_000),
    cache: "no-store",
  });
  const data = await response
    .json()
    .catch(() => ({ message: `HTTP ${response.status}` }));
  if (!response.ok)
    throw new Error(
      data.error?.message ??
        data.message ??
        data.detail ??
        `HTTP ${response.status}`,
    );
  return data as T;
}
