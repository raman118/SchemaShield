# SchemaShield

SchemaShield analyzes PostgreSQL schema migrations, explains their risks, proposes staged plans, and validates approved plans against fresh disposable PostgreSQL databases.

Its policy engine parses SQL with PostgreSQL's AST, applies versioned deterministic rules, and produces explainable findings and safety scores. Optional AI can suggest migration ideas, but it cannot change policy, approve a plan, or execute SQL. People review and approve artifacts after validation.

> SchemaShield is a local developer tool with a deliberately limited execution surface. Fixture results do not prove production compatibility or guarantee zero downtime. Destructive changes and application cutover require manual work.

## What it does

- Parses PostgreSQL migration statements and rejects unsupported or ambiguous syntax.
- Reports versioned policy findings, source locations, evidence, mitigations, and score deductions.
- Builds staged plans for supported changes, including concurrent indexes and validated constraints.
- Validates approved, hash-bound artifacts in owned disposable PostgreSQL environments.
- Records lock, performance, validation, and audit evidence.
- Demonstrates resumable batched backfill for one reviewed email-to-shadow-column mapping.
- Provides a web dashboard for analysis, planning, validation, approval, execution, backfill, and job history.

## Quick start

### Requirements

- Python 3.12 or later, with [uv](https://docs.astral.sh/uv/)
- Node.js 20.9 or later and npm
- Docker Compose and a trusted local Docker engine

The sandbox Compose overlay mounts the Docker socket into the API container. Use it only with a Docker engine you control. The migration database is disposable and isolated from the application's control database.

### Install and run

```sh
uv sync --frozen --extra dev
npm ci --prefix frontend
cp .env.example .env
```

Edit `.env` and replace the example API token and database password with private values before enabling sandbox execution. Then start the services:

```sh
docker pull postgres:17.11-bookworm
docker compose -f compose.yaml -f compose.sandbox.yaml up --build -d --wait
```

Open the dashboard at [http://localhost:3000](http://localhost:3000). The API health and readiness endpoints are at `/health` and `/ready`; interactive API documentation is at `/docs` on port 8000. Enter the configured API token in the dashboard connection settings. The browser keeps it in page memory and does not write it to browser storage.

To stop the services and preserve control-database history:

```sh
docker compose -f compose.yaml -f compose.sandbox.yaml down
```

Do not add `--volumes` unless you intend to delete the Compose control database and its history. For the API without runtime migration validation, start with `compose.yaml` alone; analysis remains available while sandbox execution is disabled.

## Verify

Run the local gates:

```sh
uv run python scripts/verify.py
```

Run the full integration, browser, runtime, audit, and Compose gates:

```sh
uv run python scripts/verify.py --full
```

The full run needs Docker, registry access, and free local ports 3000 and 8000. It requires real PostgreSQL integration tests and reports unavailable infrastructure as a blocker rather than treating simulation as a pass. See [evaluation methods and recorded evidence](docs/EVALUATION.md) for conditions, sample sizes, measurements, and limitations.

## Security model and limits

- Never point migration execution at the control database, production, or a user-selected database URL. Execution targets only owned disposable environments.
- The trusted API is the only service with Docker orchestration access. Disposable migration containers do not receive the Docker socket.
- Use one API worker per control database; an ownership lease rejects a competing worker.
- SQL, comments, paths, and AI output are untrusted input. Unsupported syntax fails closed.
- Policy evaluation and human approval are separate. AI has no authority to approve or execute migrations.
- Approval binds to an artifact hash and validation evidence, is single use, and expires.
- Destructive SQL is blocked; SchemaShield does not attempt automatic reverse migrations as recovery.
- The backfill engine supports a narrow reviewed example. General transformations, application dual writes, cutover, and contract steps remain manual.

Do not expose the Docker socket or API to untrusted users. Do not commit `.env`, credentials, certificates, or private keys.

## Project map

| Path | Purpose |
| --- | --- |
| `backend/schemashield/` | FastAPI service, PostgreSQL parser, policy, planner, persistence, approvals, and sandbox |
| `frontend/` | Next.js dashboard and browser workflows |
| `alembic/` | Control-database schema migrations |
| `fixtures/` | Small labeled policy-evaluation dataset |
| `tests/` | Unit, security-boundary, and real PostgreSQL integration tests |
| `scripts/verify.py` | Local and full verification entry point |
| `compose.yaml` and `compose.sandbox.yaml` | Local services and opt-in sandbox configuration |
| `docs/DEMO.md` | Step-by-step workflow and reproducible demonstration |
| `docs/EVALUATION.md` and `reports/` | Evaluation approach and recorded reports |
| `ARCHITECTURE.md` | Architecture, trust boundaries, and system contracts |

## Further reading

- [Reproduce the demo](docs/DEMO.md)
- [Evaluation results and limitations](docs/EVALUATION.md)
- [Architecture and trust boundaries](ARCHITECTURE.md)
- [Technical decisions](DECISIONS.md)
