"""Generate reviewable alternatives. Human mappings and cutover are real gates."""

from pglast import ast, parse_sql

from .contracts import Analysis, Plan, PlanStep
from .parser import quote_ident, render, sql_hash

TIMEOUT_PRECONDITIONS = [
    "PostgreSQL 17 and exact target schema confirmed in an owned disposable environment.",
    "Executor applies lock_timeout = 1s and statement_timeout = 5s per connection.",
    "All generated SQL must be analyzed and tested as a new artifact before approval.",
]


def generate_plan(analysis: Analysis) -> Plan:
    plan = Plan(
        eligible=False,
        artifact_hash=analysis.artifact_hash,
        reason="",
        warnings=[
            "This is an unverified alternative bound to the source SQL hash, not execution authorization.",
            "No zero-downtime guarantee; lock and concurrency behavior require real measurements.",
            "Failure recovery requires inspection; destructive SQL is never automatically reversed.",
        ],
    )
    operations = analysis.parsed.operations
    if not analysis.parsed.valid or not operations or any(not op.supported for op in operations):
        plan.reason = "Invalid or unsupported SQL cannot produce an executable plan."
        return plan
    steps = []

    def step(
        phase: str,
        description: str,
        sql: str | None = None,
        *,
        transaction: str = "transactional",
        approval: bool = False,
        preconditions: list[str] | None = None,
        verification: list[str] | None = None,
    ) -> None:
        steps.append(
            PlanStep(
                id=f"step-{len(steps) + 1:03d}",
                phase=phase,
                description=description,
                sql=sql,
                transaction=transaction,
                requires_approval=approval,
                preconditions=TIMEOUT_PRECONDITIONS + (preconditions or []),
                verification=verification or [],
            )
        )

    for op in operations:
        table, details = op.table, op.details
        column = quote_ident(op.column) if op.column else None
        if details.get("cascade") or op.kind in {
            "drop_table",
            "drop_column",
            "drop_index",
            "drop_constraint",
            "drop_not_null",
        }:
            plan.reason = "Destructive removals require an independently designed deprecation and recovery plan; no generic safe transformation is available."
            return plan
        if details.get("if_exists") or details.get("if_not_exists"):
            plan.reason = "Conditional DDL requires catalog definition evidence before a plan can be generated."
            return plan
        if op.kind == "create_index":
            node = parse_sql(op.sql)[0].stmt
            if not isinstance(node, ast.IndexStmt) or not node.idxname:
                plan.reason = (
                    "Concurrent index plans require an explicit, collision-checked index name."
                )
                return plan
            node.concurrent = True
            index_name = node.idxname.replace("'", "''")
            step(
                "EXPAND",
                "Build index concurrently without an explicit transaction block.",
                render(node) + ";",
                transaction="autocommit",
                preconditions=[
                    "Index is absent; target is an ordinary non-partitioned table; no conflicting concurrent index build.",
                    "Duplicates and application uniqueness expectations reviewed.",
                ],
                verification=[
                    "Inspect pg_index.indisvalid and indisready; failed builds can leave an invalid index."
                ],
            )
            step(
                "VERIFY",
                "Inspect the created index definition and validity.",
                f"SELECT ns.nspname, idx.relname, i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class idx ON idx.oid = i.indexrelid JOIN pg_namespace ns ON ns.oid = idx.relnamespace WHERE idx.relname = '{index_name}';",
                verification=[
                    "Match the expected namespace, table, definition and uniqueness; require exactly one intended index, indisvalid and indisready true."
                ],
            )
        elif op.kind == "add_constraint" and details.get("constraint_kind") in {"check", "foreign"}:
            if not details.get("name"):
                plan.reason = (
                    "Constraint plans require an explicit name for deterministic validation."
                )
                return plan
            # Reparse the original command to preserve the full PostgreSQL AST.
            original = parse_sql(op.sql)[0].stmt
            matching_constraints = [
                c.def_
                for c in original.cmds
                if isinstance(c.def_, ast.Constraint) and c.def_.conname == details["name"]
            ]
            if len(matching_constraints) != 1:
                plan.reason = "Ambiguous constraint identity in the statement."
                return plan
            constraint = matching_constraints[0]
            constraint.skip_validation = True
            constraint.initially_valid = False
            definition = render(constraint)
            constraint_name = quote_ident(details["name"])
            step(
                "EXPAND",
                "Add constraint without scanning historical rows; new writes are still checked.",
                f"ALTER TABLE {table} ADD {definition};",
                preconditions=[
                    "Constraint name is absent; current application writes already satisfy the constraint."
                ],
            )
            step(
                "VERIFY",
                "Validate historical rows separately from constraint installation.",
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint_name};",
                verification=[
                    "Require successful validation and pg_constraint.convalidated = true for the exact table and constraint."
                ],
            )
        elif op.kind == "set_not_null":
            name = "ss_nn_" + sql_hash(f"{table}.{op.column}")[:12]
            constraint = quote_ident(name)
            step(
                "EXPAND",
                "Install a temporary NOT VALID CHECK enforcing non-null new writes.",
                f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({column} IS NOT NULL) NOT VALID;",
                preconditions=[
                    "Application no longer writes NULL; temporary constraint name is absent."
                ],
            )
            step(
                "BACKFILL",
                "Resolve historical NULL rows using a human-reviewed mapping and bounded resumable batches.",
                transaction="manual",
                preconditions=[
                    "Human supplies mapping, immutable unique batch key, dual-write strategy and failure recovery."
                ],
            )
            step(
                "VERIFY",
                "Validate the non-null CHECK on all historical rows.",
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint};",
                verification=[
                    "Require convalidated true and zero NULL rows under application concurrency."
                ],
            )
            step(
                "CONTRACT",
                "Set NOT NULL after catalog proof and explicit approval; retain CHECK until independently reviewed removal.",
                f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL;",
                approval=True,
                preconditions=[
                    "Valid CHECK must prove this exact column non-null; source PostgreSQL version confirmed.",
                    "Application deployment and backfill completion explicitly approved.",
                ],
            )
        elif op.kind == "alter_column_type":
            replacement = (
                (op.column or "column").encode("utf-8")[:35].decode("utf-8", errors="ignore")
                + "__ss_"
                + sql_hash(f"{table}.{op.column}.{details['type']}")[:8]
            )
            target = quote_ident(replacement)
            step(
                "EXPAND",
                "Add a nullable replacement column while retaining the application-facing source.",
                f"ALTER TABLE {table} ADD COLUMN {target} {details['type']};",
                preconditions=[
                    "Replacement column is absent; inspect indexes, triggers, foreign keys, defaults and dependencies."
                ],
            )
            step(
                "EXPAND",
                "Deploy reviewed dual writes and compatibility reads; no generated trigger or cast is trusted automatically.",
                transaction="manual",
                preconditions=[
                    "Human reviews source-to-target mapping, rejected values and concurrent-write semantics.",
                    "All deployed application versions preserve source and replacement values.",
                ],
            )
            step(
                "BACKFILL",
                "Configure bounded resumable backfill with durable checkpoints and an immutable unique batch key.",
                transaction="manual",
                preconditions=[
                    "Reviewed mapping and concurrency conflict handling supplied; no cast inferred from target type alone."
                ],
            )
            step(
                "VERIFY",
                "Count unmigrated non-null source records; additional mapping-specific equality checks are mandatory.",
                f"SELECT count(*) AS unmigrated FROM {table} WHERE {column} IS NOT NULL AND {target} IS NULL;",
                verification=[
                    "Require zero unmigrated rows; human-authored mapping comparisons, null semantics, record counts and concurrent updates must also pass."
                ],
            )
            step(
                "CONTRACT",
                "Switch application dependencies to the replacement column and retire obsolete references.",
                transaction="manual",
                approval=True,
                preconditions=[
                    "Approved application cutover, mapping verification and observed compatibility window completed."
                ],
            )
            step(
                "CONTRACT",
                "Drop the original column only after explicit destructive approval and verified recovery preparation.",
                f"ALTER TABLE {table} DROP COLUMN {column};",
                transaction="manual",
                approval=True,
                preconditions=[
                    "No application or catalog dependency references the original column.",
                    "Human reviewed restorable backups; verified evidence is bound to this exact contract artifact.",
                ],
            )
        elif op.kind == "add_column":
            if details.get("not_null") or details.get("constraints"):
                plan.reason = "Column additions with inline defaults or constraints require separate catalog-aware staging."
                return plan
            step(
                "EXPAND",
                "Add nullable column as a bounded metadata change.",
                f"ALTER TABLE {table} ADD COLUMN {column} {details['type']};",
                preconditions=[
                    "Existing application tolerates the additive schema and target column is absent."
                ],
                verification=[
                    "Verify exact type, nullability and continued application read/write behavior."
                ],
            )
        elif op.kind == "create_table":
            step(
                "EXPAND",
                "Create an additive table after dependency and name checks.",
                op.sql.rstrip(";") + ";",
                preconditions=[
                    "Table is absent; referenced table constraints and application compatibility reviewed."
                ],
                verification=[
                    "Inspect exact columns, defaults, constraints and foreign-key references."
                ],
            )
        elif op.kind == "validate_constraint":
            step(
                "VERIFY",
                "Validate the named existing constraint with a bounded runtime.",
                f"ALTER TABLE {table} VALIDATE CONSTRAINT {quote_ident(details['name'])};",
                preconditions=[
                    "Constraint exists on the exact table and new application writes satisfy it."
                ],
                verification=[
                    "Inspect pg_constraint.convalidated and concurrent workload disruption."
                ],
            )
        else:
            plan.reason = f"No reviewed staged transformation for operation {op.kind}."
            return plan
    plan.eligible = True
    plan.reason = (
        "Eligible for review as a staged alternative; manual steps prevent unattended execution."
    )
    plan.steps = steps
    return plan
