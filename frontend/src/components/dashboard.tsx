"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  Activity,
  ArrowRight,
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  Code2,
  Database,
  FlaskConical,
  History,
  Layers3,
  Loader2,
  LockKeyhole,
  PanelLeft,
  Play,
  RefreshCw,
  ShieldCheck,
  Terminal,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  API_URL,
  request,
  type Audit,
  type Capabilities,
  type Job,
  type Migration,
} from "@/lib/api";

const example =
  "-- A potentially disruptive change to an existing table\nALTER TABLE accounts ALTER COLUMN balance TYPE bigint;";
const sections = [
  { id: "workspace", label: "Migration workspace", icon: Code2 },
  { id: "history", label: "Migration history", icon: History },
  { id: "jobs", label: "Backfill & jobs", icon: Activity },
  { id: "audit", label: "Audit trail", icon: LockKeyhole },
];
function list<T>(value: T[] | { items: T[] }): T[] {
  return Array.isArray(value) ? value : (value.items ?? []);
}
function date(value: string) {
  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: string;
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}
function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <Database size={28} aria-hidden />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

export function Dashboard() {
  const [token, setToken] = useState("local-development-token-change-me");
  const [view, setView] = useState("workspace");
  const [tab, setTab] = useState("analysis");
  const [title, setTitle] = useState("Change account balance type");
  const [sql, setSql] = useState(example);
  const [migrations, setMigrations] = useState<Migration[]>([]);
  const [selected, setSelected] = useState<Migration | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [audits, setAudits] = useState<Audit[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [connected, setConnected] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const actionLock = useRef(false);
  const [activeJob, setActiveJob] = useState<string | null>(null);
  const [recommendation, setRecommendation] = useState<{
    status: string;
    proposal?: { explanation: string; candidate_sql: string };
    candidate_analysis?: unknown;
    message?: string;
  } | null>(null);
  const load = useCallback(async () => {
    const [records, caps, audit, jobRecords] = await Promise.all([
      request<Migration[] | { items: Migration[] }>("/api/migrations", token),
      request<Capabilities>("/api/capabilities", token),
      request<Audit[] | { items: Audit[] }>("/api/audit", token),
      request<Job[] | { items: Job[] }>("/api/jobs", token),
    ]);
    setMigrations(list(records));
    setCapabilities(caps);
    setAudits(list(audit));
    setJobs(list(jobRecords));
    setConnected(true);
  }, [token]);
  const run = useCallback(
    async (label: string, action: () => Promise<void>) => {
      if (actionLock.current) return;
      actionLock.current = true;
      setPending(label);
      setError("");
      setNotice("");
      try {
        await action();
      } catch (e) {
        setError(e instanceof Error ? e.message : "The request failed.");
      } finally {
        setPending(null);
        actionLock.current = false;
      }
    },
    [],
  );
  useEffect(() => {
    let cancelled = false;
    request("/health", token)
      .then(() => {
        if (!cancelled) setConnected(true);
      })
      .catch(() => {
        if (!cancelled) setConnected(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);
  const refreshSelected = useCallback(
    async (id: string) => {
      const record = await request<Migration>(`/api/migrations/${id}`, token);
      setSelected(record);
      await load();
      return record;
    },
    [token, load],
  );
  useEffect(() => {
    if (!activeJob) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const job = await request<Job>(`/api/jobs/${activeJob}`, token);
        if (stopped) return;
        setJobs((previous) => [
          job,
          ...previous.filter((item) => item.id !== job.id),
        ]);
        if (!["queued", "running"].includes(job.status)) {
          const record = await request<Migration>(
            `/api/migrations/${job.migration_id}`,
            token,
          );
          if (stopped) return;
          setSelected((previous) =>
            previous?.id === record.id ? record : previous,
          );
          await load();
          if (stopped) return;
          if (job.status === "passed")
            setNotice(
              "Job passed. Actual integrity checks and measurements are available in the report.",
            );
          else
            setError(
              `Job ${job.status}. Review its report and recovery evidence.`,
            );
          setActiveJob(null);
          return;
        }
      } catch (e) {
        if (!stopped) {
          setError(e instanceof Error ? e.message : "Job polling failed.");
          setActiveJob(null);
        }
        return;
      }
      if (!stopped) timer = setTimeout(poll, 1500);
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [activeJob, token, load]);
  async function submit() {
    await run("Analyzing migration", async () => {
      const record = await request<Migration>("/api/migrations", token, {
        title,
        sql,
      });
      setSelected(record);
      setRecommendation(null);
      setTab("analysis");
      await load();
      setNotice("Migration parsed and evaluated by deterministic policies.");
    });
  }
  async function mutate(action: string) {
    if (!selected) return;
    await run(
      action === "plan"
        ? "Generating staged plan"
        : "Validating in disposable PostgreSQL",
      async () => {
        const result = await request<Job>(
          `/api/migrations/${selected.id}/${action}`,
          token,
          action === "validate" ? { target: "plan" } : {},
        );
        if (action === "validate") setActiveJob(result.id);
        await refreshSelected(selected.id);
        setTab(action === "plan" ? "plan" : "validation");
        setNotice(
          action === "plan"
            ? "Plan generated. Review all preconditions and application handoffs."
            : "Validation job submitted. Waiting for actual PostgreSQL results.",
        );
      },
    );
  }
  const analysis = selected?.analysis;
  const findings = analysis?.findings ?? [];
  const latest = selected?.reports?.findLast(
    (r) => r.kind === "validation" || r.kind === "execution",
  );
  const passingReport = selected?.reports?.findLast(
    (r) =>
      r.status === "passed" &&
      r.kind === "validation" &&
      r.artifact_hash === (selected.plan?.plan_hash ?? analysis?.artifact_hash),
  );
  const approval = selected?.approvals?.findLast(
    (a) => !a.consumed && a.artifact_hash === passingReport?.artifact_hash,
  );
  const sandbox =
    !!capabilities?.sandbox_available &&
    capabilities?.sandbox_enabled !== false;
  const blocked = migrations.filter(
    (m) => m.analysis.decision === "blocked",
  ).length;
  const tested = migrations.filter((m) =>
    m.reports?.some((r) => r.kind === "validation" && r.status === "passed"),
  ).length;
  const busy = !!pending;
  const inputStale =
    !!selected && (selected.sql !== sql || selected.title !== title);
  return (
    <div className="app-shell">
      <a href="#main" className="skip-link">
        Skip to workspace
      </a>
      <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
        <Link className="brand" href="/" aria-label="SchemaShield home">
          <span className="brand-mark">
            <ShieldCheck size={25} />
          </span>
          <span>
            SchemaShield<small>POSTGRESQL SAFETY ENGINE</small>
          </span>
        </Link>
        <div className="workspace-pill">
          <span className="workspace-icon">S</span>
          <span>
            Local workspace<small>Disposable environments only</small>
          </span>
          <ChevronRight size={14} />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Main navigation">
          {sections.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => {
                setView(id);
                setMobileNav(false);
              }}
              className={`nav-item ${view === id ? "active" : ""}`}
              aria-current={view === id ? "page" : undefined}
            >
              <Icon size={18} />
              {label}
              {id === "workspace" && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="policy-card">
            <ShieldCheck size={20} />
            <strong>Safety by design</strong>
            <p>
              Deterministic rules decide.
              <br />
              Tests verify. Humans approve.
            </p>
            <span>PostgreSQL 17 · Local execution</span>
          </div>
          <div className="profile">
            <span className="avatar">LE</span>
            <span>
              Local engineer<small>Development workspace</small>
            </span>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <Button
              variant="ghost"
              className="mobile-toggle"
              aria-label="Toggle navigation"
              onClick={() => setMobileNav(!mobileNav)}
            >
              <PanelLeft size={19} />
            </Button>
            <span>Workspace</span>
            <ChevronRight size={14} />
            <strong>{sections.find((s) => s.id === view)?.label}</strong>
          </div>
          <div className="topbar-status">
            <span className={`status-dot ${connected ? "online" : ""}`} />
            {connected ? "API reachable" : "API disconnected"}
            <span className="vertical-divider" />
            <Badge>LOCAL</Badge>
          </div>
        </header>
        <main id="main">
          <div className="page-heading">
            <div>
              <div className="eyebrow">SCHEMA EVOLUTION, UNDER CONTROL</div>
              <h1>
                {view === "workspace"
                  ? "Migration workspace"
                  : sections.find((s) => s.id === view)?.label}
              </h1>
              <p>
                {view === "workspace"
                  ? "Understand the risk. Build a safer path. Verify every change."
                  : "Evidence and activity from your local SchemaShield backend."}
              </p>
            </div>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => run("Refreshing workspace", load)}
            >
              <RefreshCw size={16} className={busy ? "spin" : ""} />
              Refresh workspace
            </Button>
          </div>
          <details className="connection-settings">
            <summary>
              <LockKeyhole size={14} /> API connection & authentication{" "}
              <span>{API_URL}</span>
            </summary>
            <div>
              <label htmlFor="api-token">
                Local API bearer token
                <input
                  id="api-token"
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value);
                    setConnected(false);
                  }}
                />
              </label>
              <p>
                Enter the token configured on your backend, then refresh. The
                token stays in this page’s memory.
              </p>
            </div>
          </details>
          {error && (
            <div className="alert error" role="alert">
              <CircleAlert size={18} />
              <span>{error}</span>
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="alert success" role="status">
              <Check size={18} />
              <span>{notice}</span>
            </div>
          )}
          {pending && (
            <div className="alert loading" role="status">
              <Loader2 className="spin" size={18} />
              {pending}…
            </div>
          )}
          <div className="stats-grid">
            <Card className="stat">
              <div>
                <span>Migrations analyzed</span>
                <Code2 size={17} />
              </div>
              <strong>{migrations.length}</strong>
              <small>Saved in this workspace</small>
            </Card>
            <Card className="stat">
              <div>
                <span>Unsafe changes blocked</span>
                <ShieldCheck size={17} />
              </div>
              <strong>{blocked}</strong>
              <small>Deterministic policy decisions</small>
            </Card>
            <Card className="stat">
              <div>
                <span>Passing validation</span>
                <FlaskConical size={17} />
              </div>
              <strong>{tested}</strong>
              <small>Actual persisted passing reports</small>
            </Card>
            <Card className="stat">
              <div>
                <span>Disposable PostgreSQL</span>
                <Database size={17} />
              </div>
              <strong className="stat-text">
                {capabilities
                  ? sandbox
                    ? "Available"
                    : "Unavailable"
                  : "Not checked"}
              </strong>
              <small>
                {capabilities?.reason ??
                  "Refresh to check runtime capabilities"}
              </small>
            </Card>
          </div>
          {view === "workspace" && (
            <>
              <div className="workflow-strip">
                <span className="workflow-active">
                  <Code2 size={15} />
                  01 · Analyze
                </span>
                <ArrowRight size={14} />
                <span>
                  <Layers3 size={15} />
                  02 · Plan
                </span>
                <ArrowRight size={14} />
                <span>
                  <FlaskConical size={15} />
                  03 · Validate
                </span>
                <ArrowRight size={14} />
                <span>
                  <ShieldCheck size={15} />
                  04 · Approve & execute
                </span>
              </div>
              <div className="workspace-grid">
                <Card className="editor-card">
                  <CardHeader>
                    <div>
                      <span className="section-icon">
                        <Terminal size={18} />
                      </span>
                      <CardTitle>Migration SQL</CardTitle>
                    </div>
                    <Badge>PostgreSQL 17</Badge>
                  </CardHeader>
                  <CardContent>
                    <label htmlFor="migration-title">
                      Migration name
                      <input
                        id="migration-title"
                        maxLength={120}
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                      />
                    </label>
                    <div className="editor-toolbar">
                      <span>
                        <span className="file-dot" /> migration.sql
                      </span>
                      <span>UTF-8 · SQL</span>
                    </div>
                    <label className="sr-only" htmlFor="migration-sql">
                      PostgreSQL migration SQL
                    </label>
                    <textarea
                      id="migration-sql"
                      spellCheck={false}
                      className="sql-editor"
                      value={sql}
                      maxLength={100000}
                      onChange={(e) => setSql(e.target.value)}
                    />
                    <div className="editor-footer">
                      <span>
                        {sql.split("\n").length} lines ·{" "}
                        {new TextEncoder().encode(sql).length} bytes
                      </span>
                      <span>PostgreSQL AST parser</span>
                    </div>
                    <div className="editor-actions">
                      <Button
                        disabled={
                          busy || !title.trim() || !sql.trim() || !token
                        }
                        onClick={submit}
                      >
                        {busy ? (
                          <Loader2 size={16} className="spin" />
                        ) : (
                          <ShieldCheck size={16} />
                        )}
                        Analyze migration
                        <ArrowRight size={16} />
                      </Button>
                      <p>Analysis never executes your SQL.</p>
                    </div>
                    {inputStale && (
                      <p className="small-warning">
                        The editor changed. Analyze again to create a new
                        artifact before planning or testing.
                      </p>
                    )}
                  </CardContent>
                </Card>
                <Card className="score-card">
                  <CardHeader>
                    <div>
                      <span className="section-icon">
                        <ShieldCheck size={18} />
                      </span>
                      <CardTitle>Safety assessment</CardTitle>
                    </div>
                    <Badge
                      tone={
                        analysis?.decision === "blocked" ? "danger" : "neutral"
                      }
                    >
                      {analysis
                        ? analysis.decision === "blocked"
                          ? "Execution blocked"
                          : "Review required"
                        : "Awaiting analysis"}
                    </Badge>
                  </CardHeader>
                  <CardContent>
                    {analysis ? (
                      <>
                        <div
                          className={`score-ring ${analysis.score.value < 50 ? "score-danger" : ""}`}
                          style={
                            {
                              "--score": `${analysis.score.value}%`,
                            } as React.CSSProperties
                          }
                        >
                          <div>
                            <strong>
                              {analysis.score.value}
                              <small>/ 100</small>
                            </strong>
                            <span>Safety score</span>
                          </div>
                        </div>
                        <p className="score-description">
                          {analysis.score.explanation}
                        </p>
                        <div className="score-legend">
                          <span>Higher is safer</span>
                          <span>{analysis.score.policy_version}</span>
                        </div>
                        <div className="score-facts">
                          <div>
                            <span>Execution policy</span>
                            <strong>
                              {analysis.decision === "blocked"
                                ? "Blocked"
                                : "Human review"}
                            </strong>
                          </div>
                          <div>
                            <span>Runtime evidence</span>
                            <strong>
                              {passingReport
                                ? "Passing local report"
                                : "Not verified"}
                            </strong>
                          </div>
                          <div>
                            <span>Detected findings</span>
                            <strong>{findings.length}</strong>
                          </div>
                        </div>
                      </>
                    ) : (
                      <Empty title="Start with a migration">
                        Submit PostgreSQL SQL to see a reproducible score and
                        the evidence behind it.
                      </Empty>
                    )}
                  </CardContent>
                </Card>
              </div>
              <Tabs value={tab} onValueChange={setTab} className="results">
                <TabsList aria-label="Migration results">
                  <TabsTrigger value="analysis">
                    <CircleAlert size={16} />
                    Risk analysis
                    {findings.length > 0 && (
                      <span className="count">{findings.length}</span>
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="plan">
                    <Layers3 size={16} />
                    Staged plan
                  </TabsTrigger>
                  <TabsTrigger value="validation">
                    <FlaskConical size={16} />
                    Validation & execution
                  </TabsTrigger>
                  <TabsTrigger value="locks">
                    <Activity size={16} />
                    Lock & performance
                  </TabsTrigger>
                </TabsList>
                <TabsContent value="analysis">
                  <Card>
                    <CardHeader>
                      <div>
                        <CardTitle>Deterministic findings</CardTitle>
                        <p>
                          Every decision traces back to a versioned rule and a
                          SQL location.
                        </p>
                      </div>
                      <Badge>{analysis?.policy_version ?? "No artifact"}</Badge>
                    </CardHeader>
                    <CardContent>
                      {!analysis ? (
                        <Empty title="No analysis yet">
                          Analyze your first migration to inspect risks and
                          score deductions.
                        </Empty>
                      ) : (
                        <>
                          {analysis.parsed.diagnostics.map((d, i) => (
                            <div className="alert error" key={i}>
                              <CircleAlert size={16} />
                              {d.message}
                            </div>
                          ))}
                          {findings.length === 0 ? (
                            <p>
                              No policy findings. This migration still requires
                              runtime validation and explicit approval.
                            </p>
                          ) : (
                            <div className="findings">
                              {findings.map((f, i) => (
                                <details
                                  className="finding"
                                  key={`${f.rule_id}-${i}`}
                                  open={i === 0}
                                >
                                  <summary>
                                    <span
                                      className={`finding-symbol ${f.severity}`}
                                    >
                                      <CircleAlert size={17} />
                                    </span>
                                    <div>
                                      <strong>{f.explanation}</strong>
                                      <span>
                                        {f.rule_id} · v{f.rule_version} · line{" "}
                                        {f.location.line}:{f.location.column}
                                      </span>
                                    </div>
                                    <Badge
                                      tone={
                                        ["critical", "high"].includes(
                                          f.severity,
                                        )
                                          ? "danger"
                                          : "warning"
                                      }
                                    >
                                      {f.severity}
                                    </Badge>
                                    <ChevronRight size={16} />
                                  </summary>
                                  <div className="finding-body">
                                    <h3>Recommended mitigation</h3>
                                    <p>{f.mitigation}</p>
                                    <h3>Rule evidence · {f.category}</h3>
                                    <Json value={f.evidence} />
                                  </div>
                                </details>
                              ))}
                            </div>
                          )}
                          <div className="analysis-bottom">
                            <div>
                              <h3>Explainable score deductions</h3>
                              {analysis.score.contributions.map((c, i) => (
                                <div className="deduction" key={i}>
                                  <span>
                                    <strong>{c.category}</strong>
                                    <small>{c.reason}</small>
                                  </span>
                                  <b>−{c.deduction}</b>
                                </div>
                              ))}
                            </div>
                            <div>
                              <h3>Conservative assumptions</h3>
                              <ul className="assumptions">
                                {analysis.assumptions.map((a, i) => (
                                  <li key={i}>{a}</li>
                                ))}
                              </ul>
                              <h3>Parsed operations</h3>
                              {analysis.parsed.operations.map((op, i) => (
                                <div className="operation" key={i}>
                                  <Badge
                                    tone={op.supported ? "neutral" : "danger"}
                                  >
                                    {op.kind}
                                  </Badge>
                                  <span>
                                    {op.table ?? "schema"}
                                    {op.column ? `.${op.column}` : ""}
                                  </span>
                                </div>
                              ))}
                              <p className="muted">
                                {analysis.parsed.parser_version}
                              </p>
                            </div>
                          </div>
                          <details className="artifact">
                            <summary>Artifact identity & source hash</summary>
                            <code>{analysis.artifact_hash}</code>
                            <p>
                              Report and approval identities must match the
                              exact artifact.
                            </p>
                          </details>
                        </>
                      )}
                    </CardContent>
                  </Card>
                </TabsContent>
                <TabsContent value="plan">
                  <Card>
                    <CardHeader>
                      <div>
                        <CardTitle>
                          Expand → Backfill → Verify → Contract
                        </CardTitle>
                        <p>
                          Review application handoffs and verification gates
                          before any execution.
                        </p>
                      </div>
                      <Button
                        disabled={busy || !selected || inputStale}
                        onClick={() => mutate("plan")}
                      >
                        <Layers3 size={16} />
                        Generate safer plan
                      </Button>
                    </CardHeader>
                    <CardContent>
                      {!selected?.plan ? (
                        <Empty title="Build a safer path">
                          Generate a deterministic plan for eligible schema
                          changes. Unsupported transformations are rejected
                          explicitly.
                        </Empty>
                      ) : (
                        <>
                          <div
                            className={`alert ${selected.plan.eligible ? "success" : "error"}`}
                          >
                            <CircleAlert size={17} />
                            {selected.plan.reason}
                          </div>
                          {selected.plan.warnings.map((w, i) => (
                            <p className="small-warning" key={i}>
                              {w}
                            </p>
                          ))}
                          <div className="plan-timeline">
                            {selected.plan.steps.map((step, i) => (
                              <div className="plan-step" key={step.id}>
                                <div className="step-number">
                                  {String(i + 1).padStart(2, "0")}
                                </div>
                                <div>
                                  <div className="step-heading">
                                    <Badge
                                      tone={
                                        step.phase === "CONTRACT"
                                          ? "warning"
                                          : "neutral"
                                      }
                                    >
                                      {step.phase}
                                    </Badge>
                                    <span>{step.transaction}</span>
                                    {step.requires_approval && (
                                      <Badge tone="warning">
                                        Human approval
                                      </Badge>
                                    )}
                                  </div>
                                  <h3>{step.description}</h3>
                                  {step.sql && (
                                    <pre className="sql-block">{step.sql}</pre>
                                  )}
                                  {step.preconditions.length > 0 && (
                                    <>
                                      <h4>Preconditions</h4>
                                      <ul>
                                        {step.preconditions.map((p, j) => (
                                          <li key={j}>{p}</li>
                                        ))}
                                      </ul>
                                    </>
                                  )}
                                  {step.verification.length > 0 && (
                                    <>
                                      <h4>Verification</h4>
                                      <ul>
                                        {step.verification.map((p, j) => (
                                          <li key={j}>
                                            <code>{p}</code>
                                          </li>
                                        ))}
                                      </ul>
                                    </>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        </>
                      )}
                    </CardContent>
                  </Card>
                </TabsContent>
                <TabsContent value="validation">
                  <div className="validation-grid">
                    <Card>
                      <CardHeader>
                        <div>
                          <CardTitle>Disposable validation console</CardTitle>
                          <p>
                            Only real PostgreSQL reports establish runtime
                            verification.
                          </p>
                        </div>
                        <Button
                          disabled={
                            busy ||
                            !!activeJob ||
                            !selected ||
                            !selected.plan?.eligible ||
                            !sandbox ||
                            inputStale
                          }
                          onClick={() => mutate("validate")}
                        >
                          <Play size={15} />
                          Validate plan
                        </Button>
                      </CardHeader>
                      <CardContent>
                        {!sandbox && (
                          <div className="alert warning">
                            <CircleAlert size={18} />
                            {capabilities?.reason ??
                              "Refresh capabilities to check Docker availability."}
                          </div>
                        )}
                        {latest ? (
                          <>
                            <div className="report-heading">
                              <Badge
                                tone={
                                  ["passed", "success", "succeeded"].includes(
                                    latest.status,
                                  )
                                    ? "success"
                                    : "warning"
                                }
                              >
                                {latest.status}
                              </Badge>
                              <span>Report {latest.id}</span>
                            </div>
                            {latest.error && (
                              <div className="alert error">{latest.error}</div>
                            )}
                            <h3>Verification checks</h3>
                            <Json value={latest.checks} />
                            <details className="artifact">
                              <summary>Validated artifact hash</summary>
                              <code>{latest.artifact_hash}</code>
                            </details>
                          </>
                        ) : (
                          <div className="console-empty">
                            <Terminal size={20} />
                            <span>No runtime report collected.</span>
                            <p>
                              Validation creates a disposable database, runs
                              eligible stages, verifies results, and cleans up
                              the environment.
                            </p>
                          </div>
                        )}
                      </CardContent>
                    </Card>
                    <Card>
                      <CardHeader>
                        <CardTitle>Review & controlled execution</CardTitle>
                        <LockKeyhole size={18} />
                      </CardHeader>
                      <CardContent>
                        <ol className="approval-list">
                          <li>
                            <Check size={15} />
                            Plan must be eligible
                          </li>
                          <li>
                            <Check size={15} />
                            Passing report must match artifact
                          </li>
                          <li>
                            <Check size={15} />
                            Human approval is recorded
                          </li>
                          <li>
                            <Check size={15} />
                            Execution stays in disposable PostgreSQL
                          </li>
                        </ol>
                        <label htmlFor="reviewer">
                          Reviewer name
                          <input
                            id="reviewer"
                            maxLength={100}
                            value={reviewer}
                            onChange={(e) => setReviewer(e.target.value)}
                            placeholder="Enter your name"
                          />
                        </label>
                        <Button
                          variant="outline"
                          className="full-width"
                          disabled={
                            busy ||
                            !!activeJob ||
                            !sandbox ||
                            !selected?.plan?.eligible ||
                            !passingReport ||
                            !reviewer.trim() ||
                            inputStale
                          }
                          onClick={() =>
                            run("Recording approval", async () => {
                              if (!selected || !passingReport) return;
                              await request(
                                `/api/migrations/${selected.id}/approve`,
                                token,
                                {
                                  artifact_hash: passingReport.artifact_hash,
                                  report_id: passingReport.id,
                                  reviewer: reviewer.trim(),
                                },
                              );
                              await refreshSelected(selected.id);
                              setNotice(
                                "Human approval recorded for the verified artifact.",
                              );
                            })
                          }
                        >
                          <ShieldCheck size={16} />
                          Approve verified plan
                        </Button>
                        {approval && (
                          <p className="muted">
                            Latest approval: {approval.reviewer}
                          </p>
                        )}
                        <Button
                          className="full-width"
                          disabled={
                            busy ||
                            !!activeJob ||
                            !sandbox ||
                            !approval ||
                            !passingReport ||
                            inputStale
                          }
                          onClick={() =>
                            run("Executing approved local plan", async () => {
                              if (!selected || !approval) return;
                              const job = await request<Job>(
                                `/api/migrations/${selected.id}/execute`,
                                token,
                                { approval_id: approval.id },
                              );
                              setActiveJob(job.id);
                              await refreshSelected(selected.id);
                              setNotice(
                                "Execution response saved. Inspect the actual report and audit events.",
                              );
                            })
                          }
                        >
                          <Play size={16} />
                          Execute in disposable database
                        </Button>
                        <p className="muted">
                          Failure reports include recovery guidance. Destructive
                          SQL is never automatically reversed.
                        </p>
                      </CardContent>
                    </Card>
                  </div>
                </TabsContent>
                <TabsContent value="locks">
                  <Card>
                    <CardHeader>
                      <div>
                        <CardTitle>Lock & concurrency measurements</CardTitle>
                        <p>
                          Collected under controlled traffic in disposable
                          PostgreSQL.
                        </p>
                      </div>
                      <Badge>EMPIRICAL EVIDENCE</Badge>
                    </CardHeader>
                    <CardContent>
                      {latest &&
                      Object.keys(latest.metrics ?? {}).length > 0 ? (
                        <Json value={latest.metrics} />
                      ) : (
                        <Empty title="No measurements available">
                          Run local validation with Docker enabled to collect
                          timings, lock samples, query disruptions, and workload
                          conditions. Static risk predictions are shown in Risk
                          analysis.
                        </Empty>
                      )}
                    </CardContent>
                  </Card>
                </TabsContent>
              </Tabs>
            </>
          )}
          {view === "history" && (
            <Card>
              <CardHeader>
                <CardTitle>Saved migrations</CardTitle>
                <Badge>{migrations.length} records</Badge>
              </CardHeader>
              <CardContent>
                {migrations.length === 0 ? (
                  <Empty title="No saved migrations">
                    Analyze SQL or refresh to load persisted migrations.
                  </Empty>
                ) : (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Migration</th>
                          <th>Decision</th>
                          <th>Safety</th>
                          <th>Submitted</th>
                          <th>
                            <span className="sr-only">Action</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {migrations.map((m) => (
                          <tr key={m.id}>
                            <td>
                              <strong>{m.title}</strong>
                              <small>{m.id}</small>
                            </td>
                            <td>
                              <Badge
                                tone={
                                  m.analysis.decision === "blocked"
                                    ? "danger"
                                    : "warning"
                                }
                              >
                                {m.analysis.decision.replaceAll("_", " ")}
                              </Badge>
                            </td>
                            <td>{m.analysis.score.value}/100</td>
                            <td>{date(m.created_at)}</td>
                            <td>
                              <Button
                                variant="ghost"
                                disabled={busy}
                                onClick={() =>
                                  run("Loading migration", async () => {
                                    const record = await request<Migration>(
                                      `/api/migrations/${m.id}`,
                                      token,
                                    );
                                    setSelected(record);
                                    setRecommendation(null);
                                    setTitle(record.title);
                                    setSql(record.sql);
                                    setView("workspace");
                                    setTab("analysis");
                                  })
                                }
                              >
                                Open
                                <ArrowRight size={15} />
                              </Button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </CardContent>
            </Card>
          )}
          {view === "jobs" && (
            <>
              <Card className="backfill-demo-card">
                <CardHeader>
                  <div>
                    <CardTitle>Fixed-fixture backfill recovery demo</CardTitle>
                    <p>
                      Copy accounts.email → email_shadow in bounded batches,
                      interrupt, resume, and verify in disposable PostgreSQL.
                    </p>
                  </div>
                  <Button
                    disabled={busy || !sandbox || !selected || !!activeJob}
                    onClick={() =>
                      run(
                        "Starting interruption and recovery demo",
                        async () => {
                          if (!selected) return;
                          const job = await request<Job>(
                            `/api/migrations/${selected.id}/backfill-demo`,
                            token,
                            { batch_size: 100, interrupt_after_batches: 2 },
                          );
                          setActiveJob(job.id);
                          await load();
                          setNotice(
                            "Fixed-fixture backfill demo submitted. Real progress will be collected from the job.",
                          );
                        },
                      )
                    }
                  >
                    <Play size={15} />
                    Run backfill recovery demo
                  </Button>
                </CardHeader>
                <CardContent>
                  <p className="muted">
                    {!selected
                      ? "Analyze or open a migration first to associate the demo with an audit record."
                      : !sandbox
                        ? "A working, explicitly enabled Docker sandbox is required."
                        : "This demonstration uses only the server-owned fixture dataset."}
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <div>
                    <CardTitle>Backfill progress & execution jobs</CardTitle>
                    <p>
                      Persistent job state and recovery evidence from the
                      backend.
                    </p>
                  </div>
                  <Badge>{jobs.length} jobs</Badge>
                </CardHeader>
                <CardContent>
                  {jobs.length === 0 ? (
                    <Empty title="No active or saved jobs">
                      Jobs appear when the backend starts supported backfills or
                      controlled execution. Refresh to load current progress.
                    </Empty>
                  ) : (
                    <div className="job-list">
                      {jobs.map((job) => (
                        <article key={job.id} className="job">
                          <div>
                            <h3>{job.kind ?? "Execution job"}</h3>
                            <Badge>{job.status}</Badge>
                          </div>
                          <p className="muted">{job.id}</p>
                          {job.progress && <Json value={job.progress} />}
                          {job.report?.progress && (
                            <Json value={job.report.progress} />
                          )}
                          <details>
                            <summary>Job state & recovery data</summary>
                            <Json value={job} />
                          </details>
                          {job.error && (
                            <p className="small-warning">{job.error}</p>
                          )}
                          {["pending", "running", "queued"].includes(
                            job.status,
                          ) && (
                            <Button
                              variant="destructive"
                              disabled={busy}
                              onClick={() =>
                                run("Cancelling job", async () => {
                                  await request(
                                    `/api/jobs/${job.id}/cancel`,
                                    token,
                                    {},
                                  );
                                  await load();
                                  setNotice("Cancellation request recorded.");
                                })
                              }
                            >
                              <X size={16} />
                              Cancel job
                            </Button>
                          )}
                          <Button
                            variant="outline"
                            disabled={busy}
                            onClick={() =>
                              run("Refreshing job", async () => {
                                const updated = await request<Job>(
                                  `/api/jobs/${job.id}`,
                                  token,
                                );
                                setJobs((previous) =>
                                  previous.map((item) =>
                                    item.id === job.id ? updated : item,
                                  ),
                                );
                              })
                            }
                          >
                            <RefreshCw size={15} />
                            Refresh job
                          </Button>
                        </article>
                      ))}
                    </div>
                  )}
                </CardContent>
              </Card>
            </>
          )}
          {view === "audit" && (
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>Immutable activity trail</CardTitle>
                  <p>
                    Analysis, plan changes, validation, approval, and execution
                    events.
                  </p>
                </div>
                <Badge>{audits.length} events</Badge>
              </CardHeader>
              <CardContent>
                {audits.length === 0 ? (
                  <Empty title="No audit events loaded">
                    Refresh the workspace to retrieve actual persisted activity.
                  </Empty>
                ) : (
                  <div className="audit-list">
                    {audits.map((a) => (
                      <article key={a.id}>
                        <span className="audit-icon">
                          <Clock3 size={16} />
                        </span>
                        <div>
                          <h3>
                            {a.event
                              .replaceAll("_", " ")
                              .replaceAll(".", " · ")}
                          </h3>
                          <p>
                            {date(a.created_at)}
                            {a.subject_id ? ` · ${a.subject_id}` : ""}
                          </p>
                          {a.details != null && (
                            <details>
                              <summary>Event evidence</summary>
                              <Json value={a.details} />
                            </details>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          )}
          {view === "workspace" && (
            <Card className="recommendation-card">
              <CardHeader>
                <div>
                  <CardTitle>Optional AI recommendations</CardTitle>
                  <p>
                    Unverified proposals require deterministic analysis, tests,
                    and human approval.
                  </p>
                </div>
                <Button
                  variant="outline"
                  disabled={busy || !selected || inputStale}
                  onClick={() =>
                    run("Requesting optional recommendation", async () => {
                      if (!selected) return;
                      const result = await request<{
                        status: string;
                        proposal?: {
                          explanation: string;
                          candidate_sql: string;
                        };
                        candidate_analysis?: unknown;
                        message?: string;
                      }>(`/api/migrations/${selected.id}/recommend`, token, {});
                      setRecommendation(result);
                    })
                  }
                >
                  Request recommendation
                </Button>
              </CardHeader>
              <CardContent>
                {recommendation ? (
                  <>
                    <Badge
                      tone={
                        recommendation.status === "unverified_proposal"
                          ? "warning"
                          : "neutral"
                      }
                    >
                      {recommendation.status.replaceAll("_", " ")}
                    </Badge>
                    <p className="muted">
                      {recommendation.message ??
                        (recommendation.status === "disabled"
                          ? "AI is disabled on this backend. Deterministic analysis and planning remain fully functional."
                          : "Provider output carries no approval or execution authority.")}
                    </p>
                    {recommendation.proposal && (
                      <>
                        <p>{recommendation.proposal.explanation}</p>
                        <pre className="sql-block">
                          {recommendation.proposal.candidate_sql}
                        </pre>
                        <details>
                          <summary>Deterministic candidate analysis</summary>
                          <Json value={recommendation.candidate_analysis} />
                        </details>
                      </>
                    )}
                  </>
                ) : (
                  <p className="muted">
                    An optional server-configured provider can propose
                    explanations and candidate SQL. Generated SQL is never
                    executed directly.
                  </p>
                )}
              </CardContent>
            </Card>
          )}
          <footer className="page-footer">
            <span>
              <ShieldCheck size={14} />
              SchemaShield · Deterministic PostgreSQL migration safety
            </span>
            <span>
              AI may propose. Rules decide. Tests verify. Humans approve.
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}
