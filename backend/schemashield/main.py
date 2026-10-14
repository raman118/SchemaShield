import asyncio
import hashlib
import hmac
import json
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select, text, update

from .ai import HTTPJSONProvider, recommend
from .backfill import run_backfill_demo
from .config import Settings, get_settings
from .contracts import Analysis, ApprovalRequest, Artifact, Plan, SubmitMigration
from .execution_guard import ExecutionRejected, guard_sql
from .planner import generate_plan
from .policy import analyze
from .sandbox import SandboxRunner
from .store import Approval, Audit, Job, Migration, create_store, new_id

logger = logging.getLogger("schemashield")
WORKER_LEASE = 1396918356
REQUEST_BODY_TIMEOUT_SECONDS = 5


class WorkerConflict(RuntimeError):
    """A second API worker cannot recover another worker's live jobs."""


class TargetRequest(Artifact):
    target: str = Field(default="migration", pattern="^(migration|plan)$")


class ExecuteRequest(Artifact):
    approval_id: str


class BackfillDemoRequest(Artifact):
    batch_size: int = Field(default=100, ge=1, le=1000)
    interrupt_after_batches: int = Field(default=2, ge=1, le=100)


def digest_plan(plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_app(settings: Settings | None = None, runner: SandboxRunner | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine, sessions = create_store(settings)
    runner = runner or SandboxRunner(settings)
    ai_provider = (
        HTTPJSONProvider(settings.ai_gateway_url, settings.ai_gateway_token.get_secret_value())
        if settings.ai_gateway_url and settings.ai_gateway_token
        else None
    )
    job_lock = threading.Lock()
    lease_lock = threading.Lock()
    lease_state = {"connection": None, "healthy": settings.database_url.startswith("sqlite:")}

    def worker_available():
        if settings.database_url.startswith("sqlite:"):
            return True
        with lease_lock:
            if not lease_state["healthy"] or lease_state["connection"] is None:
                return False
            try:
                held = (
                    lease_state["connection"]
                    .execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                            "AND pid=pg_backend_pid() AND classid=0 AND objid=:key AND objsubid=1)"
                        ),
                        {"key": WORKER_LEASE},
                    )
                    .scalar_one()
                )
                if held:
                    return True
            except Exception:
                pass
            lease_state["healthy"] = False
            logger.error(json.dumps({"event": "worker_lease_lost"}))
            return False

    @asynccontextmanager
    async def lifespan(app):
        lease = None
        if not settings.database_url.startswith("sqlite:"):
            lease = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            try:
                if not lease.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LEASE}
                ).scalar_one():
                    raise WorkerConflict("Only one API worker may own this control database")
                lease_state.update(connection=lease, healthy=True)
                app.state.worker_lease_pid = lease.execute(
                    text("SELECT pg_backend_pid()")
                ).scalar_one()
            except BaseException:
                lease.close()
                engine.dispose()
                raise
        reaper_stop = threading.Event()

        def reap():
            while not reaper_stop.is_set():
                runner.cleanup_expired()
                reaper_stop.wait(5)

        reaper = None
        if settings.sandbox_enabled and hasattr(runner, "cleanup_expired"):
            reaper = threading.Thread(target=reap, daemon=True)
            reaper.start()
        # An interrupted job is failed, never silently replayed or marked successful.
        try:
            with sessions.begin() as session:
                interrupted = session.scalars(
                    select(Job).where(Job.status.in_(["queued", "running"]))
                ).all()
                for job in interrupted:
                    job.status = "failed"
                    job.report = {
                        "status": "failed",
                        "artifact_hash": job.artifact_hash,
                        "error": "Service restart interrupted this job; review partial steps; no automatic replay",
                        "checks": [],
                        "metrics": {},
                    }
                    session.add(
                        Audit(
                            event="job.interrupted", subject_id=job.id, details={"kind": job.kind}
                        )
                    )
        except Exception:
            logger.warning(json.dumps({"event": "persistence_unavailable", "request_id": None}))
        try:
            yield
        finally:
            reaper_stop.set()
            if reaper:
                reaper.join(timeout=6)
            if lease:
                try:
                    if lease_state["healthy"]:
                        lease.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LEASE}
                        )
                finally:
                    lease.close()
                    lease_state.update(connection=None, healthy=False)
            engine.dispose()

    app = FastAPI(title="SchemaShield", version="0.1.0", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.runner = runner
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.cors_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def bounded_request(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        if (
            request.url.path.startswith("/api/") or request.url.path == "/ready"
        ) and not worker_available():
            return JSONResponse(
                status_code=503,
                content={
                    "code": "worker_unavailable",
                    "message": "Worker ownership lost; restart this API worker",
                    "request_id": request.state.request_id,
                },
            )
        if request.method == "POST":
            # Bound actual streamed body, including chunked requests without Content-Length.
            body = bytearray()
            try:
                async with asyncio.timeout(REQUEST_BODY_TIMEOUT_SECONDS):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 120_000:
                            return JSONResponse(
                                status_code=413,
                                content={
                                    "code": "payload_too_large",
                                    "message": "Request exceeds 120000 bytes",
                                    "request_id": request.state.request_id,
                                },
                            )
            except TimeoutError:
                return JSONResponse(
                    status_code=408,
                    content={
                        "code": "request_timeout",
                        "message": "Request body deadline exceeded",
                        "request_id": request.state.request_id,
                    },
                )
            request._body = bytes(body)
        started = datetime.now(UTC)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": (datetime.now(UTC) - started).total_seconds() * 1000,
                }
            )
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": f"http_{exc.status_code}",
                "message": str(exc.detail),
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic error objects may contain submitted secrets/SQL. Return only safe field paths.
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_request",
                "message": "Invalid or unexpected request fields",
                "fields": [".".join(str(p) for p in e["loc"]) for e in exc.errors()],
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        logger.error(
            json.dumps(
                {
                    "event": "request_failed",
                    "request_id": request.state.request_id,
                    "exception_type": type(exc).__name__,
                }
            )
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": "service_unavailable",
                "message": "Service dependency unavailable; inspect local service health",
                "request_id": request.state.request_id,
            },
        )

    def auth(request: Request):
        if not worker_available():
            raise HTTPException(503, "Worker ownership lost; restart required")
        token = request.headers.get("authorization", "")
        if not hmac.compare_digest(token.encode(), ("Bearer " + settings.api_token).encode()):
            raise HTTPException(401, "A valid local API token is required")

    def get_migration(session, migration_id):
        record = session.get(Migration, migration_id)
        if record is None:
            raise HTTPException(404, "Migration not found")
        return record

    def job_data(job):
        return {
            "id": job.id,
            "migration_id": job.migration_id,
            "kind": job.kind,
            "status": job.status,
            "artifact_hash": job.artifact_hash,
            "target": job.target,
            "created_at": job.created_at.isoformat(),
            "report": job.report,
            "cancel_requested": job.cancel_requested,
            "progress": (job.report or {}).get("progress"),
        }

    def migration_data(session, record):
        jobs = session.scalars(
            select(Job).where(Job.migration_id == record.id).order_by(Job.created_at)
        ).all()
        approvals = session.scalars(
            select(Approval).where(Approval.migration_id == record.id)
        ).all()
        plan = {**record.plan, "plan_hash": digest_plan(record.plan)} if record.plan else None
        return {
            "id": record.id,
            "title": record.title,
            "sql": record.sql,
            "created_at": record.created_at.isoformat(),
            "analysis": record.analysis,
            "plan": plan,
            "reports": [
                {
                    **(j.report or {}),
                    "id": j.id,
                    "status": j.status,
                    "artifact_hash": j.artifact_hash,
                    "target": j.target,
                    "kind": j.kind,
                }
                for j in jobs
            ],
            "approvals": [
                {
                    "id": a.id,
                    "artifact_hash": a.artifact_hash,
                    "reviewer": a.reviewer,
                    "report_id": a.report_id,
                    "consumed": a.consumed,
                }
                for a in approvals
            ],
        }

    def execution_artifact(record, target):
        fresh = analyze(record.sql)
        if fresh.artifact_hash != record.artifact_hash or fresh.model_dump() != record.analysis:
            raise HTTPException(
                409, "SQL content or policy artifact has changed; resubmit and revalidate"
            )
        if target == "migration":
            analysis = fresh
            if analysis.decision == "blocked":
                raise HTTPException(
                    409,
                    "Deterministic policy blocks this migration; generate and review a supported alternative",
                )
            sql = [record.sql]
            digest = record.artifact_hash
        else:
            if not record.plan:
                raise HTTPException(409, "Generate a plan first")
            plan = Plan.model_validate(record.plan)
            if (
                plan.artifact_hash != record.artifact_hash
                or record.plan != generate_plan(fresh).model_dump()
            ):
                raise HTTPException(
                    409, "Plan no longer matches the reviewed source and planner version"
                )
            if not plan.eligible or not plan.steps:
                raise HTTPException(409, "No eligible staged plan exists")
            if any(
                step.transaction == "manual"
                or (step.sql is None and step.phase != "VERIFY")
                or step.phase == "CONTRACT"
                for step in plan.steps
            ):
                raise HTTPException(
                    409,
                    "This plan requires external backfill/application cutover or contract approval; automated execution is unavailable",
                )
            # VERIFY may contain required validation DDL; intrinsic catalog checks cover
            # generated SELECT verification without permitting arbitrary read statements.
            sql = [
                step.sql
                for step in plan.steps
                if step.sql and not step.sql.lstrip().upper().startswith("SELECT")
            ]
            if not sql:
                raise HTTPException(409, "Plan has no executable DDL")
            digest = digest_plan(record.plan)
            if any(analyze(statement).decision == "blocked" for statement in sql):
                raise HTTPException(409, "Generated SQL failed deterministic reanalysis")
        try:
            statements = [item for statement in sql for item in guard_sql(statement)]
        except ExecutionRejected as exc:
            raise HTTPException(409, str(exc)) from exc
        return digest, statements

    def run_job(job_id, statements, backfill_options=None):
        with sessions.begin() as session:
            job = session.get(Job, job_id)
            if not worker_available():
                job.status = "failed"
                job.report = {
                    "status": "failed",
                    "checks": [],
                    "metrics": {},
                    "error": "Worker ownership lost before execution; restart required",
                }
                session.add(
                    Audit(
                        event="job.failed",
                        subject_id=job_id,
                        details={"reason": "worker_lease_lost"},
                    )
                )
                return
            if job.cancel_requested:
                job.status = "cancelled"
                return
            job.status = "running"
            session.add(Audit(event="job.started", subject_id=job_id, details={"kind": job.kind}))

        def cancelled():
            if not worker_available():
                return True
            with sessions() as session:
                job = session.get(Job, job_id)
                return job is None or job.cancel_requested

        try:
            if backfill_options:

                def progress(value):
                    with sessions.begin() as session:
                        job = session.get(Job, job_id)
                        job.report = {
                            "status": "running",
                            "progress": value,
                            "artifact_hash": job.artifact_hash,
                        }

                report = run_backfill_demo(
                    runner, **backfill_options, cancelled=cancelled, on_progress=progress
                )
            else:
                report = runner.run(statements, cancelled)
        except Exception:
            report = {
                "status": "failed",
                "checks": [],
                "metrics": {},
                "error": "Sandbox worker failed; cleanup attempted; review partial steps",
            }
        with sessions.begin() as session:
            job = session.get(Job, job_id)
            if not worker_available():
                report = {
                    "status": "failed",
                    "checks": [],
                    "metrics": report.get("metrics", {}),
                    "error": "Worker ownership lost; review partially committed steps; no automatic replay",
                }
            if job.cancel_requested:
                report["status"] = "cancelled"
            report["artifact_hash"] = job.artifact_hash
            report["id"] = job.id
            job.report = report
            job.status = report["status"]
            session.add(
                Audit(
                    event="job." + job.status,
                    subject_id=job_id,
                    details={"kind": job.kind, "artifact_hash": job.artifact_hash},
                )
            )

    def enqueue(session, record, target, kind, digest, tasks, statements, backfill_options=None):
        if not settings.database_url.startswith("sqlite:"):
            # Global enqueue serialization across database connections/processes.
            session.execute(text("SELECT pg_advisory_xact_lock(1396918355)"))
        active = session.scalars(select(Job).where(Job.status.in_(["queued", "running"]))).all()
        if len(active) >= settings.max_active_jobs or any(
            j.migration_id == record.id for j in active
        ):
            raise HTTPException(
                409, "An active job conflicts with this request or the local concurrency limit"
            )
        job = Job(
            id=new_id(), migration_id=record.id, kind=kind, artifact_hash=digest, target=target
        )
        session.add(job)
        session.add(
            Audit(
                event="job.queued",
                subject_id=job.id,
                details={"kind": kind, "artifact_hash": digest},
            )
        )
        session.flush()
        tasks.add_task(run_job, job.id, statements, backfill_options)
        return job_data(job)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/ready")
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return {
                "status": "ready",
                "persistence": "sqlite-development" if settings.allow_sqlite else "postgresql",
            }
        except Exception as exc:
            raise HTTPException(503, "Control persistence unavailable") from exc

    @app.get("/api/capabilities", dependencies=[Depends(auth)])
    def capabilities():
        return {
            **runner.capability(),
            "persistence": "sqlite-development" if settings.allow_sqlite else "postgresql",
            "production_execution": False,
            "ai_enabled": ai_provider is not None,
            "execution_scope": "fresh owned local disposable fixture only",
        }

    @app.post("/api/migrations", status_code=201, dependencies=[Depends(auth)])
    def submit(body: SubmitMigration):
        if len(body.sql.encode()) > 100_000:
            raise HTTPException(413, "SQL byte limit exceeded")
        result = analyze(body.sql)
        with sessions.begin() as session:
            record = Migration(
                id=new_id(),
                title=body.title,
                sql=body.sql,
                artifact_hash=result.artifact_hash,
                analysis=result.model_dump(),
            )
            session.add(record)
            session.add(
                Audit(
                    event="migration.analyzed",
                    subject_id=record.id,
                    details={
                        "artifact_hash": result.artifact_hash,
                        "decision": result.decision,
                        "policy_version": result.policy_version,
                    },
                )
            )
            session.flush()
            return migration_data(session, record)

    @app.get("/api/migrations", dependencies=[Depends(auth)])
    def history():
        with sessions() as session:
            return [
                migration_data(session, r)
                for r in session.scalars(
                    select(Migration).order_by(Migration.created_at.desc()).limit(100)
                ).all()
            ]

    @app.post("/api/migrations/{migration_id}/recommend", dependencies=[Depends(auth)])
    def recommendation(migration_id: str):
        with sessions.begin() as session:
            record = get_migration(session, migration_id)
            result = recommend(record.sql, ai_provider)
            session.add(
                Audit(
                    event="ai.proposal_requested",
                    subject_id=record.id,
                    details={"status": result["status"], "artifact_hash": record.artifact_hash},
                )
            )
            return result

    @app.post(
        "/api/migrations/{migration_id}/backfill-demo",
        status_code=202,
        dependencies=[Depends(auth)],
    )
    def backfill_demo(
        migration_id: str, tasks: BackgroundTasks, body: BackfillDemoRequest | None = None
    ):
        options = (body or BackfillDemoRequest()).model_dump()
        with job_lock, sessions.begin() as session:
            record = get_migration(session, migration_id)
            if not runner.capability()["sandbox_available"]:
                raise HTTPException(
                    503, "Disposable PostgreSQL unavailable; backfill demonstration cannot run"
                )
            digest = hashlib.sha256(
                json.dumps(
                    {"fixture_transform": "accounts-email-shadow-v1", **options}, sort_keys=True
                ).encode()
            ).hexdigest()
            return enqueue(session, record, "fixture-demo", "backfill", digest, tasks, [], options)

    @app.get("/api/migrations/{migration_id}", dependencies=[Depends(auth)])
    def detail(migration_id: str):
        with sessions() as session:
            return migration_data(session, get_migration(session, migration_id))

    @app.post("/api/migrations/{migration_id}/plan", dependencies=[Depends(auth)])
    def plan(migration_id: str):
        with sessions.begin() as session:
            record = get_migration(session, migration_id)
            candidate = generate_plan(Analysis.model_validate(record.analysis)).model_dump()
            record.plan = candidate
            session.add(
                Audit(
                    event="plan.generated",
                    subject_id=record.id,
                    details={
                        "plan_hash": digest_plan(candidate),
                        "eligible": candidate["eligible"],
                    },
                )
            )
            return {**candidate, "plan_hash": digest_plan(candidate)}

    @app.post(
        "/api/migrations/{migration_id}/validate", status_code=202, dependencies=[Depends(auth)]
    )
    def validate(migration_id: str, tasks: BackgroundTasks, body: TargetRequest | None = None):
        target = body.target if body else "migration"
        with job_lock, sessions.begin() as session:
            record = get_migration(session, migration_id)
            digest, statements = execution_artifact(record, target)
            if not runner.capability()["sandbox_available"]:
                raise HTTPException(
                    503, "Disposable PostgreSQL unavailable; no validation report can be produced"
                )
            return enqueue(session, record, target, "validation", digest, tasks, statements)

    @app.post(
        "/api/migrations/{migration_id}/approve", status_code=201, dependencies=[Depends(auth)]
    )
    def approve(migration_id: str, body: ApprovalRequest):
        with job_lock, sessions.begin() as session:
            record = get_migration(session, migration_id)
            evidence = session.get(Job, body.report_id)
            if (
                evidence is None
                or evidence.migration_id != record.id
                or evidence.kind != "validation"
                or evidence.status != "passed"
                or not evidence.report
            ):
                raise HTTPException(
                    409, "Successful validation evidence for this artifact is required"
                )
            digest, _ = execution_artifact(record, evidence.target)
            age = datetime.now(UTC) - evidence.created_at.replace(tzinfo=UTC)
            if (
                digest != body.artifact_hash
                or evidence.artifact_hash != digest
                or age > timedelta(hours=1)
            ):
                raise HTTPException(
                    409, "Validation evidence is stale or the artifact digest does not match"
                )
            checks = evidence.report.get("checks", [])
            if (
                evidence.report.get("artifact_hash") != digest
                or evidence.report.get("status") != "passed"
                or not checks
                or not all(c.get("passed") is True for c in checks)
            ):
                raise HTTPException(409, "Complete successful integrity checks are required")
            approval = Approval(
                id=new_id(),
                migration_id=record.id,
                artifact_hash=digest,
                report_id=evidence.id,
                reviewer=body.reviewer,
            )
            session.add(approval)
            session.add(
                Audit(
                    event="artifact.approved",
                    subject_id=record.id,
                    details={
                        "approval_id": approval.id,
                        "reviewer": body.reviewer,
                        "artifact_hash": digest,
                        "report_id": evidence.id,
                    },
                )
            )
            session.flush()
            return {"id": approval.id, "artifact_hash": digest, "reviewer": body.reviewer}

    @app.post(
        "/api/migrations/{migration_id}/execute", status_code=202, dependencies=[Depends(auth)]
    )
    def execute(migration_id: str, body: ExecuteRequest, tasks: BackgroundTasks):
        with job_lock, sessions.begin() as session:
            record = get_migration(session, migration_id)
            approval = session.get(Approval, body.approval_id)
            if approval is None or approval.migration_id != record.id or approval.consumed:
                raise HTTPException(409, "An unused approval for this migration is required")
            evidence = session.get(Job, approval.report_id)
            if (
                not evidence
                or evidence.status != "passed"
                or evidence.kind != "validation"
                or evidence.migration_id != record.id
                or not evidence.report
                or evidence.report.get("artifact_hash") != approval.artifact_hash
                or evidence.report.get("status") != "passed"
                or not evidence.report.get("checks")
                or not all(c.get("passed") is True for c in evidence.report["checks"])
            ):
                raise HTTPException(409, "Successful validation is required")
            digest, statements = execution_artifact(record, evidence.target)
            if (
                digest != approval.artifact_hash
                or digest != evidence.artifact_hash
                or datetime.now(UTC) - evidence.created_at.replace(tzinfo=UTC) > timedelta(hours=1)
            ):
                raise HTTPException(409, "Approval or validation evidence is stale")
            if not runner.capability()["sandbox_available"]:
                raise HTTPException(503, "Disposable PostgreSQL unavailable")
            changed = session.execute(
                update(Approval)
                .where(Approval.id == approval.id, Approval.consumed.is_(False))
                .values(consumed=True)
            ).rowcount
            if changed != 1:
                raise HTTPException(409, "Approval was already consumed")
            return enqueue(session, record, evidence.target, "execution", digest, tasks, statements)

    @app.get("/api/jobs", dependencies=[Depends(auth)])
    def jobs():
        with sessions() as session:
            return [
                job_data(j)
                for j in session.scalars(
                    select(Job).order_by(Job.created_at.desc()).limit(100)
                ).all()
            ]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def job_detail(job_id: str):
        with sessions() as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, "Job not found")
            return job_data(job)

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    def cancel(job_id: str):
        with sessions.begin() as session:
            job = session.get(Job, job_id)
            if not job:
                raise HTTPException(404, "Job not found")
            if job.status not in {"queued", "running"}:
                raise HTTPException(409, "Job has already finished")
            job.cancel_requested = True
            session.add(Audit(event="job.cancel_requested", subject_id=job_id, details={}))
            return job_data(job)

    @app.get("/api/audit", dependencies=[Depends(auth)])
    def audit():
        with sessions() as session:
            return [
                {
                    "id": a.id,
                    "event": a.event,
                    "subject_id": a.subject_id,
                    "details": a.details,
                    "created_at": a.created_at.isoformat(),
                }
                for a in session.scalars(
                    select(Audit).order_by(Audit.created_at.desc()).limit(200)
                ).all()
            ]

    return app


app = create_app()
