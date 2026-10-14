# SchemaShield ordered roadmap

Statuses: TODO, IN_PROGRESS, BLOCKED, DONE. Each goal requires review, documentation, a checksum checkpoint, and actual command results. A blocked infrastructure goal permits independent implementation, but dependent acceptance remains incomplete.

## G00 — Initialization and harness
Status: DONE. Dependencies: none.
Scope: inspect tools; persistent documents; backend/frontend structure; Python 3.12 environment; registry-pinned dependencies; lint/test setup; Docker Compose definition; SHA-256 snapshots with retention and non-overwriting restore verification.
Acceptance: reproducible package installation, initial tests, formatting/lint gates, checkpoint archive verifies and restores into an empty temporary directory. Explicitly disclose absent Docker; runtime integration belongs to G02/G07.
Commands: `uv sync --extra dev`; `uv run pytest tests/test_checkpoint.py`; `uv run ruff check .`; `python scripts/checkpoint.py G00 --verify`.
Evidence: Python 3.12.13 environment and uv.lock installed; checkpoint/verification tests 21 passed; focused Ruff gates passed; .checkpoints/G00-20261012T183311546622Z-4cc3f346.zip verified and restored 46 checksums into a new temporary directory. Docker configuration provided; live runtime unavailable and explicitly assigned to integration acceptance.

## G01 — Architecture and threat model
Status: DONE. Dependencies: G00.
Scope: API contracts, AST pipeline, versioned risk/scoring, planner, isolated executor, verification, dashboard, audit and persistence schemas, trust boundaries; failure modes; alternatives and tradeoffs.
Acceptance: architecture and API align, review documents no production execution path, explain transaction restrictions and approval state transitions; architecture review recorded.
Commands: `uv run pytest tests/test_contracts.py` plus documented architecture review.
Evidence: contracts.py rejects unknown execution inputs and requires approval digest/report; 2 contract tests passed. Architecture reviewed against contracts, ownership separation, autocommit concurrent indexes, human cutover gates and no production DSN API.

## G02 — Backend foundation
Status: DONE. Dependencies: G01.
Scope: FastAPI, validated configuration, structured errors/logs, health/readiness, SQLAlchemy PostgreSQL records and audit, Alembic bootstrap, authenticated writes and integration tests.
Acceptance: starts locally; Alembic creates real PostgreSQL schema; API persists/reloads real records; isolated test suite passes; SQLite may support unit tests but cannot satisfy PostgreSQL acceptance.
Commands: `docker compose up -d db`; `uv run alembic upgrade head`; `uv run pytest tests/test_api.py tests/integration/test_persistence.py`.
Evidence: FastAPI/API tests 6 passed; real PostgreSQL persistence/restart/singleton/lease-loss tests 3 passed with Alembic. Structured errors, authenticated writes, readiness and audit verified. Full report reports/verification.json accepted.

## G03 — PostgreSQL parser
Status: DONE. Dependencies: G01; independent of live persistence.
Scope: pglast AST extraction for CREATE/ALTER/DROP TABLE, columns, types, indexes, NOT NULL, foreign keys, CHECK; multiple statements, byte locations, diagnostics; explicit unsupported nodes.
Acceptance: valid/invalid/multi-statement/quoted/unicode/unsupported/edge cases tested; unsupported subcommands cannot silently disappear.
Commands: `uv run pytest tests/test_parser.py`.
Evidence: 56 parser tests passed: invalid/multiple/quoted/Unicode/nested/unknown/cross-database operations; pglast-v8.5/extractor-1.1. Retained historical G03 archive verified before current security revisions; final snapshots capture revised source.

## G04 — Deterministic risk engine
Status: DONE. Dependencies: G03.
Scope: destructive DDL, loss, locks, rewrites, conversion, indexes, validation, compatibility and irreversibility; evidence, mitigation, location and rule versions.
Acceptance: labeled safe/unsafe cases are reproducible; unknown context conservative; AST-supported does not imply safe execution.
Commands: `uv run pytest tests/test_policy.py`.
Evidence: 32 policy tests passed; rules-1.1, finding locations/evidence/versions and conservative FK/conditional-DDL behavior verified. Static labeled evaluation TP16/TN8/FP0/FN0 on 24 cases.

## G05 — Explainable safety scoring
Status: DONE. Dependencies: G04.
Scope: transparent versioned 0–100 safety score (higher means safer); data loss/locks/compatibility/complexity/reversibility/evidence deductions and caps.
Acceptance: same inputs give byte-equivalent results; exact contributions reconcile score; missing evidence never grants tested status.
Commands: `uv run pytest tests/test_scoring.py`.
Evidence: 9 scoring tests passed; score-1.0 contributions reconcile exactly, repeat outputs match, missing runtime evidence deducts 20. Scores never authorize execution.

## G06 — Safe staged planner
Status: DONE. Dependencies: G05.
Scope: eligible EXPAND/BACKFILL/VERIFY/CONTRACT transformations; concurrent indexes; NOT VALID/VALIDATE; explicit application switch gates; bounded resumable backfill; lock/statement timeouts and transaction metadata.
Acceptance: generated SQL parses; schema assumptions and human gates explicit; unsupported transformations reject; no unconditional zero-downtime claim; contract cannot run automatically.
Commands: `uv run pytest tests/test_planner.py`.
Evidence: 24 planner tests passed; SQL parseability, concurrent-index autocommit, staged constraints and manual mapping/cutover/contract rejection verified. Runtime API executed concurrent index, CHECK and FK plans with real integrity checks.

## G07 — Disposable PostgreSQL validation
Status: DONE. Dependencies: G06, G02 acceptance for persisted reports.
Scope: owned version-pinned Docker instances, fixture loading, schema setup, restricted executor role, migration/test reports, CPU/memory/runtime limits, finally cleanup and startup orphan recovery.
Acceptance: real safe pass, unsafe block, failing SQL failure and cleanup demonstrated; no arbitrary DSN execution; machine-readable reports show actual checks.
Commands: `uv run python scripts/container_verify.py --rebuild`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: 5 real sandbox integration tests passed plus 4 real validation/approval/execution scenarios; unsafe rejection before allocation, duplicate-column failure, concurrent index validity, cancellation and owned resource cleanup verified. Internal container-network transport used.

## G08 — Lock and concurrency instrumentation
Status: DONE. Dependencies: G07.
Scope: controlled concurrent reads/writes; pg_locks/pg_stat_activity blocking samples; lock waits, timings, transactions and application errors.
Acceptance: actual controlled contention measurements recorded with sample interval/workload and cleanup; no fabricated or substituted static lock predictions.
Commands: `uv run python scripts/container_verify.py --evaluation`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: Real PostgreSQL blocking/AccessExclusiveLock integration test passed. Runtime evaluation observed deliberate lock timeout SQLSTATE 55P03 with actual pg_locks/pg_stat_activity samples; per-trial traffic/timings reported with 25-ms sampling limitations.

## G09 — Resumable backfill
Status: DONE. Dependencies: G06; real acceptance requires G07.
Scope: bounded batches, durable progress, idempotence, retry/cancel/resume, concurrent worker exclusion, concurrent change semantics, final verification.
Acceptance: real interrupted/resumed backfill conserves records and verifies values, failures persist recovery state; progress and data commit atomically.
Commands: `uv run python scripts/container_verify.py --rebuild`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: 39 backfill unit tests and 5 real PostgreSQL backfill tests passed. Interruption/reconnect, idempotence, stale verification, concurrent source rescans/NULL copy and bounded retry/runtime verified. Fixed reviewed email-shadow mapping; arbitrary transformations/application dual writes remain explicit manual planner gates.

## G10 — Dashboard
Status: DONE. Dependencies: G05; integration acceptance requires G02/G07/G09.
Scope: Next.js/React/TypeScript/Tailwind/shadcn; overview, SQL editor, risk/score/evidence, plan, validation console, lock metrics, backfill and audited history; responsive/accessibility/error/loading states.
Acceptance: real API analysis/history/plans; browser workflows; execution/approval only enabled with backend evidence; every button works or clearly reports capability unavailable.
Commands: `uv run python scripts/container_demo.py --test-browser --rebuild-api --rebuild-frontend`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: Frontend lint/format/typecheck/build and all 7 Chromium workflows passed against real PostgreSQL API and production images. Real risks/scores/plans/reports/locks/jobs/backfill/history/audit, error states, mobile navigation and WCAG checks verified. Screenshots frontend/evidence/.

## G11 — Optional AI recommendations
Status: DONE. Dependencies: G06.
Scope: provider-neutral adapter, opt-in configuration, bounded proposals, untrusted data prompt boundary, deterministic reanalysis of proposed SQL, no authority/execution connection.
Acceptance: AI-disabled workflow passes; adversarial comments cannot change decisions; provider failures handled; proposals visibly unverified.
Commands: `uv run pytest tests/test_ai.py`.
Evidence: 10 AI adapter tests passed for disabled/provider-neutral/adversarial/failure/malformed response paths. Real browser deterministic workflow passes with AI disabled. No live external provider success claimed; proposals have no approval/execution authority.

## G12 — Approval and controlled execution
Status: DONE. Dependencies: G07; implementation can proceed with mocked boundary while blocked.
Scope: review, hash-bound tested plan approval, server-held environment identity, preconditions, audit, cancellation/timeouts, status transitions and recovery.
Acceptance: no outside-local execution; approval cannot be bypassed; stale hash/evidence rejects; real approved safe execution verifies integrity; failures never imply automatic safe rollback.
Commands: `uv run python scripts/container_verify.py --rebuild`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: 15 approval unit tests and 4 real PostgreSQL execution cases passed; stale/version/hash/report/replay checks, controlled fresh environments, passing integrity checks and audit enforced. Real browser human approval/execute workflow passed.

## G13 — Hardening
Status: DONE. Dependencies: G11/G12/G10; independent adversarial tests may proceed early.
Scope: API SQL injection, malicious ASTs, size/resource/time limits, connection leaks, job races, auth, credential redaction, partial failures, prompt injection, restart recovery.
Acceptance: automated adversarial tests exercise controls; unresolved risks disclosed; no host Docker socket exposed to web browser or untrusted SQL role.
Commands: `uv run python scripts/verify.py --full`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: 27 security, 22 sandbox-boundary and build-context secret exclusion tests passed; PostgreSQL second-worker and lease-loss tests passed. Slow streamed bodies bounded at 5s. Deployed Linux Python audit: zero known vulnerabilities, editable local package explicitly skipped; npm audit: zero. Trusted orchestration/local-only limits disclosed. Follow-up hardening rejects the shipped development API token when sandbox execution is enabled; the focused security/sandbox suite passed 50 tests and the latest local full gate passed 267 unit tests. Live Docker verification was unavailable in that follow-up session and is recorded in STATE.md. Checkpoint G13-20261012T230445655118Z-097922f3.zip verified/restored; a new follow-up checkpoint is recorded in CHECKPOINTS.md. Acceptance blockers: none for the accepted scope; rerun live gates after Docker access returns.

## G14 — Evaluation
Status: DONE. Dependencies: G13; static labeled evaluation can run after G05.
Scope: safe/destructive/locking/index/compatibility/interruption/failure/traffic datasets; recall, false positives, per-rule precision; real execution/wait/recovery/error metrics.
Acceptance: repeatable JSON report with counts, conditions, confidence limitations, actual measurements and skipped scenarios. Static predictions are separate from empirical runtime results.
Commands: `uv run python scripts/container_verify.py --evaluation`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: Repeatable machine reports: 24 labeled cases x50 (1200 analyses), 18/18 empirical migration trials (15 eligible successes, 3 verified expected failures), zero skips, controlled lock timeout and backfill recovery passed. Actual metrics/sample sizes/conditions/limits in docs/EVALUATION.md and reports/.

## G15 — Deployment and full demo
Status: DONE. Dependencies: G00–G14 accepted.
Scope: local Compose, backend/frontend/isolated infrastructure, complete install/troubleshooting, examples, automated verification and reproducible performance/demo.
Acceptance: another developer reproduces unsafe submission → parsing/risks/block → staged plan → real isolated validation/metrics → explicit approval → local execution → data verification. All required quality gates pass.
Commands: `uv run python scripts/verify.py --full`; see docs/DEMO.md for local TLS environment and full gate.
Evidence: Full 14-gate verification passed: 266 unit tests, 18 real PostgreSQL tests, 7 Chromium workflows, frontend gates, static/runtime evaluation, audits and actual production Compose up/build/readiness/unsafe block/cleanup. README and docs/DEMO.md give reproducible setup including SDK path. Final snapshot verified/restored without user-data adoption.
