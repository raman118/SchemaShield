# Evaluation and measured results

Static policy classification and empirical PostgreSQL behavior are evaluated separately. Fixture success cannot establish production compatibility or guarantee zero downtime. No simulated execution results are presented as measurements.

## Reproduce and inspect

Run `uv run python scripts/verify.py --full` following [the demo setup](DEMO.md). It requires registry access and a trusted local Docker engine; this workspace uses the documented mutual TLS gateway. Individual suites are `scripts/evaluate.py`, `scripts/container_verify.py --rebuild`, `scripts/container_verify.py --evaluation`, and `scripts/container_demo.py --test-browser --rebuild-api --rebuild-frontend`.

Evidence: [full verification](../reports/verification.json), [static dataset report](../reports/static-evaluation.json), [runtime trials](../reports/runtime-evaluation.json), [PostgreSQL tests](../reports/integration-junit.xml), [browser tests](../reports/browser-junit.xml), [Compose deployment](../reports/compose-verification.json), [Python audit](../reports/python-audit.json), [frontend audit](../reports/frontend-audit.json). Screenshots are in `frontend/evidence/`.

## Static classification

Report timestamp: 2026-10-12T22:58:12.820378+00:00. Conditions: Windows-11-10.0.26200-SP0, Python 3.12.13, policy rules-1.1, parser pglast-v8.5/extractor-1.1, scoring score-1.0. The checked-in dataset is SHA-256 bound in the report.

There are 24 hand-labeled cases, 50 repeats each, and 1200 analyses. Safe means no high/critical finding under the documented assumptions; it is not execution authorization.

| Measurement | Observed |
|---|---:|
| True positives / true negatives | 16 / 8 |
| False positives / false negatives | 0 / 0 |
| Unsafe detection recall | 100.0% |
| False-positive rate | 0.0% |
| Mean / median analysis latency | 0.126 / 0.113 ms |
| p95 / maximum analysis latency | 0.264 / 0.853 ms |

Recall is TP/(TP+FN), false-positive rate FP/(FP+TN). Per-rule precision is TP/(TP+FP) against explicit annotations and is reported with counts for 21 represented rules. All repeated outputs matched deterministically. This small development corpus was authored alongside the implementation and is not independent statistical evidence. Analysis timings exclude database/network/UI work.

## Empirical PostgreSQL evaluation

Report timestamp: 2026-10-12T22:59:32.646504+00:00. PostgreSQL image postgres:17.11-bookworm, fixed 2,000 accounts and 2,000 orders. Each sandbox has 1 CPU, 384 MiB memory, 192 MiB PostgreSQL tmpfs, 1-second lock timeout and 5-second statement timeout. Server versions, resolved image IDs, fixture hashes and environment identities are recorded per report. Trials run on a local Linux Docker engine accessed from managed Windows.

Six migration scenarios repeat three times: nullable column addition, concurrent index, simple new table, staged CHECK, staged FK, and duplicate-column partial failure. A separate backfill trial pauses after committed batches, closes its connection and resumes through another session. A separate contention trial deliberately retains an ACCESS SHARE blocker and observes waiting AccessExclusiveLock plus SQLSTATE 55P03. Expected duplicate-column failures require SQLSTATE 42701 and evidence that the preceding statement committed; arbitrary infrastructure failure cannot count as success.

| Measurement | Observed |
|---|---:|
| Planned / completed migration trials | 18 / 18 |
| Skipped trials | 0 |
| Eligible migration completion | 100.0% (15 trials) |
| Mean / median successful migration latency | 5.653 / 5.187 ms |
| Maximum successful migration latency | 8.457 ms |
| Controlled workload iterations / errors | 18 / 0 |
| Controlled application error rate | 0.0% |
| Sampled deliberate contention wait | 625 ms |
| Backfill interruption/reconnect integrity | Passed |

Migration latency excludes container/fixture startup. Workload counts are read/update loop iterations, not individual SQL statements. These short migrations produced very few iterations; 0 observed errors is not a production reliability estimate. Lock wait duration is counted from 25-ms samples; sampling and observation overhead limit precision, and short locks may be missed. Full reports retain individual failures, SQLSTATEs, checks, statement timings and limitations. Each statement autocommits; failure disposal is not rollback. Backfill progress is durable only while its owned disposable database exists; verification is point-in-time and does not replace application dual writes.

## Gates and limitations

Most recent machine-readable test counts: 266 unit, 18 real PostgreSQL integration, 7 Chromium workflows. Accepted JUnit evidence has zero failures/errors/skips. Browser tests use real APIs and include WCAG checks, responsive navigation, authentication failure, stale editing, unsafe block, concurrent plan, real validation/approval/execution and backfill recovery. The Compose gate builds/starts production services, checks readiness/runtime capability/dashboard/unsafe block, then removes its unique project.

The deployed Linux Python environment audit includes 61 dependency records and 0 known vulnerabilities. The editable local SchemaShield package is explicitly skipped by advisory lookup and covered by source/security tests. npm audit reports zero known vulnerabilities. Advisory scans are time-specific, do not prove vulnerability absence, and the Windows Python advisory lookup is unavailable behind this session's network proxy.

One Starlette TestClient deprecation warning remains with the pinned httpx interface; assertions pass. Windows direct Chromium spawning and mode-0700 scratch ACLs are unsupported in this managed session; documented container and scoped scratch workflows are used. Graphify's executable lacks its Python module, so architecture reconciliation used source and tests. No external LLM provider success is claimed; optional adapter tests use bounded fake providers and the real browser workflow runs with AI disabled. Generic transformations, external application compatibility and unattended destructive production execution are outside the implemented execution subset.
