# Architecture decisions

## ADR-001 — Local execution only
Use owned disposable PostgreSQL 17 Docker instances. Reject production connection inputs. Alternative arbitrary DSNs would turn a portfolio tool into a production execution authority; excluded by scope. Docker availability is a real acceptance dependency.

Runtime patch pin: PostgreSQL 17.11-bookworm, updated from initial 17.9 after checking current supported security fixes. See [official PostgreSQL 17 security fixes](https://www.postgresql.org/support/security/17/). Runtime reports also record the resolved image ID and server version; image tags alone are not content-addressed immutable artifacts.

## ADR-002 — pglast AST
Use the PostgreSQL parser rather than regex or generic SQL dialect parsing. Preserve source byte ranges and expose unsupported nodes. Parser acceptance alone never grants execution permission.

## ADR-003 — Synchronous analysis, bounded asynchronous jobs
Analysis is deterministic and fast; run it on bounded SQL input. Execution requires durable job state, bounded concurrency and explicit cancellation/timeout. A separate worker is desirable for stronger process isolation; keep interfaces ready for it and disclose local-service trust assumptions.

## ADR-004 — Persistence versus tests
PostgreSQL is the deployed store, managed by Alembic. SQLite may support fast isolated repository tests; it cannot satisfy real PostgreSQL integration acceptance. Test skips are disclosed blockers.

## ADR-005 — Transparent safety score
Higher score means greater assessed safety. Deduct documented weighted category contributions; apply conservative caps for unknown operations and missing runtime evidence. Score is not permission. Rules, scoring and artifact hashes are versioned.

## ADR-006 — No Git
User prohibits all Git/GitHub use. Install from PyPI/npm registries; create source snapshots excluding secrets, environments, volumes and build products. Verify archives by checksums and restore into empty scratch directories. Retain five goal snapshots.

## ADR-007 — UI integrated locally
Use local Next.js with real FastAPI calls, not hosted website tooling or remote repository flows. Local Docker Compose is the deployment contract.

## ADR-008 — Isolated container network transport

Actual testing showed internal sandbox networks did not publish host ports on the inspected engine. Preserve network isolation and run trusted API/verifier containers temporarily attached to each internal network. Browser/test images receive no Docker socket. Keep an explicit local sandbox Compose overlay and use only local sockets or verified mutual TLS loopback orchestration. [Docker network documentation](https://docs.docker.com/engine/network/) describes internal networks and separate frontend/network attachments; the observed port behavior is environment-specific evidence, not a universal guarantee.

## ADR-009 — Worker ownership and restart recovery

Use a PostgreSQL session advisory lease to require one API worker per control database. Otherwise a second process could falsely mark live jobs interrupted. Transaction advisory locking and atomic approval consumption additionally protect enqueue limits and approval replay. Dedicated PostgreSQL integration verifies second-worker rejection and recovery after the original lease closes. Horizontal scaling would require a real leased worker/job protocol and is outside this local deployment.

## ADR-010 — Explicit transformation boundaries

Support automatic fixture execution for nullable additions, simple new tables, concurrent indexes and staged CHECK/FK validation within the reviewed AST subset. Replacement-column transformations require manual mapping, dual-write/cutover and contract gates. The bounded resumable engine currently implements a fixed email-shadow copy with atomic progress and consistency rescans. Arbitrary transformations and automatic production cutover are not implemented; display those gates clearly and never infer external application compatibility.

## ADR-011 — Reproducible verification under managed Windows

Use inherited temp permissions only within fresh test scratch trees to avoid sandbox token denial caused by Python mode-0700 ACLs. Next's supported worker-thread/compiler API path retains full TypeScript checks. Use pinned Linux verifier/Playwright images for real integration and browsers; fail full verification on skips/missing reports. Audit deployed Linux Python packages via registry connectivity available to that runtime; host Windows Python advisory lookup is unavailable behind this session's network proxy.
