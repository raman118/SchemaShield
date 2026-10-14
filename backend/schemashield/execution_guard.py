"""A deliberately narrow execution allowlist, distinct from AST analysis support."""

from pglast import ast, parse_sql
from pglast.enums import AlterTableType
from pglast.visitors import Visitor


class ExecutionRejected(ValueError):
    pass


class Guard(Visitor):
    def visit(self, ancestors, node):
        allowed = {
            "CreateStmt",
            "AlterTableStmt",
            "AlterTableCmd",
            "IndexStmt",
            "IndexElem",
            "RangeVar",
            "ColumnDef",
            "TypeName",
            "Constraint",
            "A_Const",
            "Integer",
            "Float",
            "String",
            "Boolean",
            "ColumnRef",
            "A_Expr",
            "BoolExpr",
            "NullTest",
            "BooleanTest",
        }
        if type(node).__name__ not in allowed:
            raise ExecutionRejected(
                f"Unreviewed expression {type(node).__name__} is not executable"
            )

    def visit_A_Expr(self, ancestors, node):
        if (
            len(node.name or ()) != 1
            or node.name[0].sval
            not in {"=", "<>", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%"}
            or node.kind.name != "AEXPR_OP"
        ):
            raise ExecutionRejected("Only reviewed unqualified operators are executable")

    def visit_RangeVar(self, ancestors, node):
        if node.catalogname or node.schemaname not in (None, "public"):
            raise ExecutionRejected("Execution is restricted to public schema objects")
        if node.relname.startswith(("pg_", "schemashield_")):
            raise ExecutionRejected("Reserved database objects cannot be changed")

    def visit_Constraint(self, ancestors, node):
        if node.contype.name == "CONSTR_FOREIGN" and (
            node.fk_del_action in {"c", "n", "d"} or node.fk_upd_action in {"c", "n", "d"}
        ):
            raise ExecutionRejected("Value-changing referential actions need manual review")

    def visit_TypeName(self, ancestors, node):
        parts = [p.sval for p in node.names]
        permitted = {
            "int2",
            "int4",
            "int8",
            "text",
            "varchar",
            "bool",
            "numeric",
            "float4",
            "float8",
            "date",
            "timestamp",
            "timestamptz",
            "uuid",
            "bytea",
        }
        if (
            not parts
            or parts[-1] not in permitted
            or (len(parts) > 1 and parts[:-1] != ["pg_catalog"])
        ):
            raise ExecutionRejected("Only reviewed built-in PostgreSQL types are executable")


def guard_sql(sql: str) -> list[str]:
    from .parser import parse_migration

    parsed = parse_migration(sql)
    if (
        not parsed.valid
        or not parsed.operations
        or any(not op.supported for op in parsed.operations)
    ):
        raise ExecutionRejected("SQL contains invalid, unmodeled or unsupported syntax")
    if any(
        op.details.get("if_not_exists") or op.details.get("if_exists") for op in parsed.operations
    ):
        raise ExecutionRejected("Conditional DDL requires exact existing catalog definitions")
    if len(sql.encode("utf-8")) > 100_000:
        raise ExecutionRejected("SQL byte limit exceeded")
    try:
        statements = parse_sql(sql)
    except Exception as exc:
        raise ExecutionRejected("Invalid PostgreSQL SQL") from exc
    if not statements or len(statements) > 20:
        raise ExecutionRejected("Execution requires between one and twenty statements")
    from pglast.stream import RawStream

    output = []
    for raw in statements:
        node = raw.stmt
        if isinstance(node, ast.CreateStmt):
            if (
                node.inhRelations
                or node.partbound
                or node.partspec
                or node.options
                or node.tablespacename
                or node.ofTypename
            ):
                raise ExecutionRejected(
                    "Inheritance, partitioning, typed tables and storage options are unsupported"
                )
            if node.relation.relpersistence != "p":
                raise ExecutionRejected("Only permanent public tables are supported")
        elif isinstance(node, ast.IndexStmt):
            if (
                not node.concurrent
                or node.whereClause
                or node.options
                or node.tableSpace
                or node.excludeOpNames
                or node.accessMethod not in (None, "btree")
            ):
                raise ExecutionRejected("Only plain btree CREATE INDEX CONCURRENTLY is executable")
            if any(param.expr or param.opclass or param.collation for param in node.indexParams):
                raise ExecutionRejected(
                    "Expression indexes and custom operator classes are not executable"
                )
        elif isinstance(node, ast.AlterTableStmt):
            allowed = {
                AlterTableType.AT_AddColumn,
                AlterTableType.AT_AddConstraint,
                AlterTableType.AT_ValidateConstraint,
            }
            if any(cmd.subtype not in allowed for cmd in node.cmds):
                raise ExecutionRejected(
                    "This ALTER TABLE command needs a manually reviewed transformation"
                )
            for cmd in node.cmds:
                if cmd.subtype == AlterTableType.AT_AddColumn:
                    if cmd.def_.is_not_null or any(
                        getattr(c.contype, "name", "") in {"CONSTR_NOTNULL", "CONSTR_PRIMARY"}
                        for c in (cmd.def_.constraints or ())
                    ):
                        raise ExecutionRejected(
                            "Direct NOT NULL addition is not an eligible local execution"
                        )
        else:
            raise ExecutionRejected(f"{type(node).__name__} is analysis-only")
        Guard()(node)
        output.append(RawStream()(node))
    return output
