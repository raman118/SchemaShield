# Reproduce SchemaShield locally

Requirements: Python 3.12+, uv, Node 20.9+ (verified with 24.15.0), npm, and a trusted local Linux Docker engine. Copy this source directory directly. Registry dependencies are pinned by `uv.lock` and `frontend/package-lock.json`; no Git or GitHub step is needed.

## Install and start

```powershell
uv sync --frozen --extra dev
npm.cmd ci --prefix frontend
docker pull postgres:17.11-bookworm
docker compose -f compose.yaml -f compose.sandbox.yaml up --build -d --wait
```

Open [the dashboard](http://localhost:3000); API health is at [health](http://localhost:8000/health), persistence readiness at [ready](http://localhost:8000/ready), and contracts at [OpenAPI](http://localhost:8000/docs). The control database uses its own `control_data` volume. Stop with the same Compose files and `down`; omit `--volumes` to preserve history. Never delete an existing user's volume for reset/recovery.

Compose reads `.env` when present. Set `CONTROL_DB_PASSWORD` and `SCHEMASHIELD_API_TOKEN` to independent long URL-safe values for an operator-specific setup. Shipped defaults are loopback development credentials. Sandbox execution refuses the shipped API token and requires a private operator-supplied value. Enter the configured API token under dashboard connection settings. It stays in page memory, without browser storage.

`compose.sandbox.yaml` explicitly gives the trusted API the local Docker socket at `/run/docker.sock`. Migration containers and browsers never receive it. Use only on a trusted local engine. Execution PostgreSQL uses an internal network that the API temporarily joins. Base Compose alone disables runtime validation while keeping analysis functional. Only one API worker may own a control database; an advisory lease rejects another worker before it can alter live job state.

## Complete demonstration

1. Submit `CREATE INDEX accounts_email_demo_idx ON accounts(email);`. Inspect **Execution blocked**, findings, byte locations, rule versions and score deductions.
2. Generate the safer plan. Inspect `CREATE INDEX CONCURRENTLY`, transaction restrictions, schema preconditions, 1-second lock timeout and 5-second statement timeout. A plan is not approval.
3. In Validation & execution, select **Validate plan**. Fresh PostgreSQL 17.11 loads 2,000 accounts and 2,000 orders, applies the reviewed artifact, measures controlled traffic and verifies original data, indexes and constraint validation.
4. Inspect Lock & performance and actual report checks. Empty 25-ms lock samples cannot prove absence of locks; fixture success cannot prove production compatibility.
5. Enter a reviewer name and approve the passing artifact. Approval binds its hash and validation report; the one-hour evidence window and single-use approval are server enforced.
6. Execute in disposable database. Another fresh fixture verifies resulting integrity, emits audit records and is removed. Execution never targets control persistence; approval replay is rejected.
7. Open Backfill & jobs and run recovery. Bounded email-to-shadow-column batches commit, the first connection closes, a new connection resumes durable progress, and all 2,000 values and original rows are checked.
8. Submit `ALTER TABLE accounts DROP COLUMN email;`. Destructive SQL is blocked. Generic removal has no automatic safe alternative or inverse-DDL recovery.

Constraint alternatives are `ALTER TABLE accounts ADD CONSTRAINT balance_nonnegative CHECK(balance >= 0);` and `ALTER TABLE orders ADD CONSTRAINT orders_account_fk FOREIGN KEY(account_id) REFERENCES accounts(id);`. Generated plans stage `NOT VALID` then `VALIDATE CONSTRAINT`.

Replacement-column plans include manual mapping, application dual-write/cutover and contract gates; automatic execution rejects them. The backfill engine demonstrates a fixed reviewed email-copy transformation, not arbitrary user SQL. Its progress survives connection/worker restart within the same disposable database, and is removed when that environment is destroyed. No zero-downtime guarantee is made.

## Verification

```powershell
uv run python scripts/verify.py
uv run python scripts/verify.py --full
```

Local gates cover lint/format, unit tests, frontend build, static evaluation and npm audit. Full gates additionally build a trusted verifier, require PostgreSQL integration with zero skips, audit deployed Linux Python dependencies, measure runtime behavior, run all seven Chromium workflows on production images, and verify actual Compose deployment. It needs free ports 3000/8000 and registry connectivity. Reports are in `reports/`; screenshots in `frontend/evidence/`. Existing projects are not adopted; only the gate's unique Compose project and its own volume are removed.

Standalone SDK commands: `scripts/container_verify.py --rebuild`, `scripts/container_verify.py --evaluation`, `scripts/container_verify.py --audit`, `scripts/container_demo.py --test-browser --rebuild-api --rebuild-frontend`, and `scripts/compose_verify.py`. Run them with `uv run python`. `container_demo.py` without the browser flag serves a disposable demo for 30 minutes, then removes its control records; `--duration` accepts 60..3600 seconds. Use Compose for persistent history.

## This managed Windows workspace

An existing verified mutual TLS Docker gateway is reachable despite absent CLI lookup and denied WSL enumeration. Its dedicated credentials in `.secrets/docker` are excluded from snapshots/build contexts. Configure without printing their contents:

```powershell
$env:DOCKER_HOST = 'tcp://127.0.0.1:2376'
$env:DOCKER_TLS_VERIFY = '1'
$env:DOCKER_CERT_PATH = Join-Path $PWD '.secrets/docker'
.venv/Scripts/python.exe scripts/verify.py --full
```

Never enable unauthenticated Docker TCP. The SDK accepts local sockets or the fixed verified mutual TLS loopback endpoint. A normal local engine uses its standard socket/pipe. The Compose gate runs the pinned official Docker CLI container if the host CLI is absent.

Windows sandbox tokens cannot read Python's newly created mode-0700 temp directories. `scripts/run_tests.py` inherits permissions only in new per-run scratch trees, without changing existing ACLs. Next uses supported worker threads and the compiler API with full type checking. Browser tests use pinned Playwright Linux because Windows child spawning is denied. Host loopback sandbox transport cannot publish ports on this internal-network engine; run the enabled API in a container.

## Troubleshooting and recovery

- Sandbox unavailable: check overlay, engine and reviewed image; never substitute a simulated pass.
- Readiness fails: inspect control PostgreSQL/Alembic. Do not execute submitted migration SQL there.
- Validation fails: inspect SQLSTATE, checks and committed statement timings. Cleanup disposes the database; it does not reverse committed DDL.
- Second worker rejected: stop the duplicate worker and allow the original connection to release its lease.
- Worker ownership lost: restart the old API process after reviewing partial jobs; it fails closed and cannot silently reacquire ownership.
- Old policy artifacts: resubmit under policy/extractor 1.1, then revalidate and approve.
- Restore: use `scripts/checkpoint.py --archive <archive> --verify`, then `--restore <new-nonexistent-directory>`, compare checksums and review before adoption. Never overwrite user data.
