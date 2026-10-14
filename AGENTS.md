# SchemaShield operating contract

## Non-negotiable instructions
Never run Git commands, initialize repositories, use GitHub, or introduce Git-dependent installation, CI, or workflows. Use package registries, local files, logs, and SHA-256 filesystem snapshots. This applies to every future session and agent.

Read this file, GOALS.md, STATE.md, and recent WORKLOG.md before work. Inspect actual files and verification evidence; documentation alone does not prove completion. Preserve user files. Never restore snapshots over user data without explicit authorization. Treat SQL, paths, comments, and AI output as untrusted.

## Engineering protocol
For each eligible goal: INSPECT → PLAN → IMPLEMENT → TEST → REVIEW → DOCUMENT → CHECKPOINT → NEXT. Use TODO, IN_PROGRESS, BLOCKED, DONE. Mark DONE only after every acceptance criterion passes. Record blockers honestly, then perform independent work. No fabricated test results, measurements, integration success, or zero-downtime guarantees.

Use Python 3.12+, FastAPI, SQLAlchemy/Alembic, PostgreSQL-aware pglast AST parsing, Next.js/TypeScript, Tailwind, accessible shadcn components, pytest, Playwright, and Docker Compose. Prefer simple explicit interfaces. Pin dependencies through lock files. Do not use regex as the SQL parser. Unknown ASTs fail closed. Policy and score versions must be included in results. Missing runtime evidence must reduce confidence.

## Security boundaries
Application persistence is distinct from disposable execution databases. Execution accepts no user-supplied database URL, Docker arguments, image, host path, or shell command. Never execute migration SQL on persistence or production databases. Only approved, tested, hash-bound artifacts may execute in owned disposable environments. AI may propose but cannot authorize, execute, or override policy. Never log credentials. Enforce SQL size, time, resource, authentication, and concurrency limits. Never automatically reverse destructive SQL as recovery.

## Tests and recovery
Run focused tests during implementation and all relevant gates before checkpoints. Integration tests require real PostgreSQL; skips are blockers for integration acceptance. Browser tests exercise actual API workflows. Evaluation reports must disclose conditions, sample size, failed/skipped scenarios, and measured values. Use scripts/checkpoint.py to snapshot source and verify archive manifests; retain five snapshots. Restore to a new empty directory, compare checksums, and review before adoption.

The user authorized coordinated senior engineering. Delegate bounded independent implementation/review tasks where useful; keep goal transitions and shared harness edits with the primary agent. Available concurrency may limit five roles to fewer simultaneous agents. Avoid overlapping file ownership.

## Future sessions
Reconcile STATE.md with files, run verification, inspect checkpoint manifests, continue the earliest unblocked goal. Do not rebuild completed features unnecessarily. Update WORKLOG.md and STATE.md at every meaningful verification or interruption. If Docker is absent, keep static analysis functional and report validation unavailable; never label simulated execution as tested.
