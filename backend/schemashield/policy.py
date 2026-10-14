"""Deterministic PostgreSQL 17 assumptions; no catalog certainty is invented."""

from .contracts import Analysis, Finding, Location
from .parser import parse_migration, sql_hash
from .scoring import calculate_score


def analyze(sql: str) -> Analysis:
    parsed = parse_migration(sql)
    findings = []

    def finding(
        rule: str,
        severity: str,
        category: str,
        location: Location,
        explanation: str,
        mitigation: str,
        evidence: dict,
    ) -> None:
        findings.append(
            Finding(
                rule_id=rule,
                severity=severity,
                category=category,
                location=location,
                explanation=explanation,
                mitigation=mitigation,
                evidence=evidence,
            )
        )

    if not parsed.valid:
        location = next(
            (d.location for d in parsed.diagnostics if d.location),
            Location(
                statement=1,
                byte_offset=0,
                byte_length=len(sql.encode("utf-8", errors="surrogatepass")),
                line=1,
                column=1,
            ),
        )
        finding(
            "PG001",
            "critical",
            "unsupported",
            location,
            "SQL cannot be safely analyzed.",
            "Correct parser diagnostics and resubmit.",
            {"diagnostics": [d.message for d in parsed.diagnostics]},
        )
    for operation in parsed.operations:
        kind, details, loc = operation.kind, operation.details, operation.location
        evidence = {
            "operation": kind,
            "table": operation.table,
            "column": operation.column,
            **details,
            "catalog_evidence": "absent",
            "postgres_assumption": "17",
        }

        def emit(
            rule: str,
            severity: str,
            category: str,
            explanation: str,
            mitigation: str,
            loc: Location = loc,
            evidence: dict = evidence,
        ) -> None:
            finding(rule, severity, category, loc, explanation, mitigation, evidence)

        if not operation.supported:
            emit(
                "PG002",
                "critical",
                "unsupported",
                "Unsupported syntax or expression cannot be authorized by this policy.",
                "Split supported operations and obtain catalog-aware manual review for the remaining syntax.",
            )
            continue
        if details.get("if_not_exists") or details.get("if_exists"):
            emit(
                "PG003",
                "medium",
                "operational",
                "Conditional DDL may silently skip incompatible existing objects; names do not prove definitions match.",
                "Assert catalog definitions explicitly before execution.",
            )
        constraints = [details] if kind == "add_constraint" else details.get("constraints", [])
        if kind == "create_table":
            constraints = constraints + [
                constraint
                for column in details.get("columns", [])
                for constraint in column.get("constraints", [])
            ]
        changing_references = [
            constraint
            for constraint in constraints
            if constraint.get("constraint_kind") == "foreign"
            and any(
                constraint.get(action) in {"cascade", "set_null", "set_default"}
                for action in ("on_delete", "on_update")
            )
        ]
        if changing_references:
            finding(
                "PG064",
                "high",
                "data_loss",
                loc,
                "Referential actions can delete or change dependent records when parent rows change; NOT VALID still enforces these actions on new operations.",
                "Review delete/update semantics, affected dependencies and application behavior before introducing cascading or value-changing foreign keys.",
                {**evidence, "referential_actions": changing_references},
            )
        if kind in {"drop_table", "drop_column"}:
            emit(
                "PG010",
                "critical",
                "data_loss",
                "Dropping a table or column destroys persisted data.",
                "Use staged deprecation, verified backups, application cutover and explicit human contract approval.",
            )
            emit(
                "PG011",
                "high",
                "reversibility",
                "Recreating a schema object cannot restore dropped data.",
                "Validate a restore procedure independently; do not assume an automatic rollback is possible.",
            )
            emit(
                "PG012",
                "high",
                "compatibility",
                "Existing application queries may reference the removed object.",
                "Verify all application versions have stopped accessing the object.",
            )
        if details.get("cascade"):
            emit(
                "PG013",
                "critical",
                "data_loss",
                "CASCADE may remove dependent objects beyond the named target.",
                "Inventory dependencies and use separately reviewed explicit operations.",
            )
        if kind in {
            "add_column",
            "drop_column",
            "drop_table",
            "alter_column_type",
            "set_not_null",
            "drop_not_null",
            "drop_constraint",
        }:
            severity = "medium" if kind in {"add_column", "drop_not_null"} else "high"
            emit(
                "PG020",
                severity,
                "lock",
                "ALTER/DROP TABLE ordinarily requires ACCESS EXCLUSIVE; even a short metadata change can wait behind transactions and block traffic.",
                "Apply a short lock_timeout and statement_timeout; test representative concurrent workloads and retry under supervision.",
            )
        if kind == "alter_column_type":
            emit(
                "PG030",
                "high",
                "rewrite",
                "The source type, casts and catalog are unknown; a type change may rewrite the table and rebuild dependent indexes.",
                "Expand with a replacement column, use reviewed bounded backfill and verify before application cutover.",
            )
            emit(
                "PG031",
                "high",
                "data_loss",
                "Unknown source type and conversion semantics can truncate, round, reject or reinterpret values.",
                "Review explicit data mapping against representative data and compare all migrated values.",
            )
            emit(
                "PG032",
                "high",
                "compatibility",
                "Changing a column type can break application codecs, parameters and query expectations.",
                "Deploy compatible dual reads/writes before any cutover.",
            )
        if kind == "add_column":
            if details.get("not_null"):
                emit(
                    "PG040",
                    "high",
                    "constraint",
                    "Existing rows may violate a new NOT NULL column; a scan or failure is possible.",
                    "Add a nullable column, backfill, validate a NOT VALID CHECK, then separately review SET NOT NULL.",
                )
            if any(
                c["constraint_kind"] in {"foreign", "check", "unique", "primary"}
                for c in details.get("constraints", [])
            ):
                emit(
                    "PG041",
                    "high",
                    "constraint",
                    "Inline constraints on an existing table may scan data or build indexes while locking traffic.",
                    "Split column addition from separately staged constraint/index creation.",
                )
            if details.get("default") is not None:
                emit(
                    "PG042",
                    "medium",
                    "operational",
                    "A literal default is often metadata-only on PostgreSQL 17, but catalog/dependency behavior and deployed server version are unverified.",
                    "Verify the server version and catalog, then measure the operation in an isolated environment.",
                )
        if kind == "set_not_null":
            emit(
                "PG043",
                "high",
                "constraint",
                "SET NOT NULL may scan the table under ACCESS EXCLUSIVE unless an eligible valid CHECK proves non-nullness.",
                "First create and validate CHECK (column IS NOT NULL); inspect catalog proof before SET NOT NULL.",
            )
        if kind in {"drop_not_null", "drop_constraint"}:
            emit(
                "PG044",
                "high",
                "compatibility",
                "Removing integrity enforcement may invalidate assumptions made by application code or dependent workflows.",
                "Review application invariants and replacement enforcement before removal.",
            )
        if kind == "create_index":
            if not details["concurrently"]:
                emit(
                    "PG050",
                    "high",
                    "lock",
                    "Ordinary CREATE INDEX takes SHARE and blocks writes for the index build.",
                    "Use CREATE INDEX CONCURRENTLY outside a transaction block, with resource limits and catalog verification.",
                )
            else:
                emit(
                    "PG051",
                    "medium",
                    "operational",
                    "Concurrent index creation performs multiple passes, waits for transactions and may leave an invalid index after failure.",
                    "Run in autocommit; inspect indisvalid/indisready, remove invalid artifacts only after review and retry.",
                )
            if details["unique"]:
                emit(
                    "PG052",
                    "medium",
                    "constraint",
                    "A unique index may fail on duplicate rows and concurrent writes; validity is unverified.",
                    "Check duplicate data, application writes and pg_index validity after the build.",
                )
        if kind == "drop_index":
            emit(
                "PG053",
                "high",
                "compatibility",
                "The index may support uniqueness or critical query performance; its owning table and dependency catalog are unknown.",
                "Inspect constraint dependencies and workload plans before removal.",
            )
            if not details["concurrently"]:
                emit(
                    "PG054",
                    "high",
                    "lock",
                    "Ordinary DROP INDEX can take ACCESS EXCLUSIVE on the indexed table.",
                    "Consider DROP INDEX CONCURRENTLY if eligible; it forbids transaction blocks and constraint-owned indexes.",
                )
        if kind == "add_constraint":
            not_valid = details.get("not_valid", False)
            emit(
                "PG060",
                "medium" if not_valid else "high",
                "constraint",
                "NOT VALID skips the initial historical-row scan but still enforces new writes."
                if not_valid
                else "Adding a validated constraint may scan historical rows and fail on existing violations.",
                "Use NOT VALID for CHECK/FOREIGN KEY where eligible, then VALIDATE in a separate controlled step.",
            )
            emit(
                "PG061",
                "medium" if not_valid else "high",
                "lock",
                "Adding a constraint still acquires table locks; foreign keys also lock the referenced table.",
                "Use short lock timeouts, separate validation and measure both referenced and referencing workloads.",
            )
        if kind == "validate_constraint":
            emit(
                "PG062",
                "medium",
                "operational",
                "VALIDATE CONSTRAINT scans historical rows under SHARE UPDATE EXCLUSIVE; concurrent DDL and resource pressure remain possible.",
                "Measure scan cost and concurrent workloads, enforce statement_timeout and inspect convalidated.",
            )
        if kind == "create_table":
            refs = [c for c in details.get("constraints", []) if c.get("references")]
            refs += [
                c
                for column in details.get("columns", [])
                for c in column.get("constraints", [])
                if c.get("references")
            ]
            if refs:
                emit(
                    "PG063",
                    "medium",
                    "lock",
                    "Foreign keys on a new table acquire locks on existing referenced tables.",
                    "Measure referenced-table lock behavior and apply lock_timeout.",
                )
    return Analysis(
        artifact_hash=sql_hash(sql),
        policy_version="rules-1.1",
        parsed=parsed,
        findings=findings,
        score=calculate_score(findings),
        decision="blocked"
        if any(f.severity in {"high", "critical"} for f in findings)
        else "review_required",
        assumptions=[
            "PostgreSQL 17 policy baseline; pglast grammar may accept newer syntax that this extractor rejects.",
            "Table sizes, source column types, existing constraints, indexes, dependencies, triggers and workloads are unknown.",
            "Static analysis never proves zero downtime; every executable artifact requires isolated validation and explicit approval.",
        ],
    )
