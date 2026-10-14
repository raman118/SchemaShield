"""PostgreSQL AST extraction. Supported syntax is deliberately narrower than PostgreSQL.

Locations are UTF-8 byte spans converted from pglast character offsets; all suboperations retain
their containing statement span. Unknown expressions and modifiers fail closed.
"""

import hashlib
from typing import Any

import pglast
from pglast import ast, parse_sql
from pglast.stream import RawStream

from .contracts import Diagnostic, Location, Operation, ParsedMigration

PARSER_VERSION = f"pglast-{pglast.__version__}/extractor-1.1"
MAX_SQL_BYTES = 100_000
MAX_OPERATIONS = 500


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_hash(sql: str) -> str:
    # Distinct invalid surrogates remain traceable as rejected artifacts.
    # Accepted PostgreSQL SQL always hashes ordinary UTF-8 bytes.
    return hashlib.sha256(sql.encode("utf-8", errors="surrogatepass")).hexdigest()


def render(node: Any) -> str:
    return RawStream()(node)


def relation_name(node: ast.RangeVar) -> str:
    parts = [part for part in (node.catalogname, node.schemaname, node.relname) if part]
    return ".".join(quote_ident(part) for part in parts)


def _relation_errors(node: ast.RangeVar) -> list[str]:
    if node.catalogname:
        return ["Database-qualified relations require catalog identity evidence"]
    return []


def _expression_errors(node: Any, depth: int = 0) -> list[str]:
    if node is None:
        return []
    if depth > 64:
        return ["Expression nesting exceeds supported depth"]
    if isinstance(node, (tuple, list)):
        return [error for child in node for error in _expression_errors(child, depth + 1)]
    if not isinstance(node, ast.Node):
        return []
    allowed = {
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
        "TypeCast",
        "TypeName",
    }
    if type(node).__name__ not in allowed:
        return [f"Unsupported expression node: {type(node).__name__}"]
    if isinstance(node, ast.A_Expr):
        operators = {"=", "<>", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%"}
        if len(node.name or ()) != 1 or node.name[0].sval not in operators:
            return ["Unsupported operator or qualified operator"]
        if node.kind.name != "AEXPR_OP":
            return [f"Unsupported expression kind: {node.kind.name}"]
    if isinstance(node, ast.TypeName):
        errors = _type_errors(node)
        if errors:
            return errors
    return [
        error
        for attribute in node
        for error in _expression_errors(getattr(node, attribute), depth + 1)
    ]


def _type_errors(node: ast.TypeName) -> list[str]:
    names = [part.sval for part in node.names or ()]
    builtins = {
        "int2",
        "int4",
        "int8",
        "integer",
        "bigint",
        "smallint",
        "text",
        "varchar",
        "bpchar",
        "bool",
        "boolean",
        "numeric",
        "decimal",
        "float4",
        "float8",
        "timestamp",
        "timestamptz",
        "date",
        "time",
        "timetz",
        "interval",
        "uuid",
        "json",
        "jsonb",
        "bytea",
    }
    if not names or names[-1] not in builtins or (len(names) > 1 and names[:-1] != ["pg_catalog"]):
        return ["Custom/domain/extension types require catalog-aware analysis"]
    if node.setof or node.pct_type:
        return ["SETOF and percent TYPE declarations are unsupported"]
    if any(
        not isinstance(modifier, ast.A_Const) or not isinstance(modifier.val, ast.Integer)
        for modifier in node.typmods or ()
    ):
        return ["Only literal integer type modifiers are supported"]
    return []


def _constraint(node: ast.Constraint) -> tuple[dict[str, Any], list[str]]:
    kind = node.contype.name.removeprefix("CONSTR_").lower()
    errors = []
    if kind not in {"null", "notnull", "default", "check", "foreign", "primary", "unique"}:
        errors.append(f"Unsupported constraint: {kind}")
    errors.extend(_expression_errors(node.raw_expr))
    if node.indexname or node.indexspace or node.options or node.exclusions:
        errors.append("Constraint index attachment, tablespaces, options or exclusions unsupported")
    if node.without_overlaps or node.fk_with_period or node.pk_with_period:
        errors.append("Temporal constraints require PostgreSQL version-specific analysis")
    if node.is_no_inherit:
        errors.append("NO INHERIT constraints unsupported")
    if kind in {"check", "foreign"} and not node.is_enforced:
        errors.append("NOT ENFORCED constraints are outside the PostgreSQL 17 policy baseline")
    if node.pktable:
        errors.extend(_relation_errors(node.pktable))
    details = {
        "constraint_kind": kind,
        "name": node.conname,
        "not_valid": bool(node.skip_validation),
        "definition": render(node),
        "references": relation_name(node.pktable) if node.pktable else None,
        "default": render(node.raw_expr) if node.raw_expr else None,
    }
    if kind == "foreign":
        actions = {
            "a": "no_action",
            "r": "restrict",
            "c": "cascade",
            "n": "set_null",
            "d": "set_default",
        }
        for attribute, key in (("fk_del_action", "on_delete"), ("fk_upd_action", "on_update")):
            value = actions.get(getattr(node, attribute))
            if value is None:
                errors.append("Unsupported foreign-key referential action")
            details[key] = value
    return details, errors


def _column(node: ast.ColumnDef) -> tuple[dict[str, Any], list[str]]:
    errors = _type_errors(node.typeName)
    errors.extend(_expression_errors(node.raw_default))
    constraints = []
    for constraint in node.constraints or ():
        details, unsupported = _constraint(constraint)
        constraints.append(details)
        errors.extend(unsupported)
    if (
        node.collClause
        or node.fdwoptions
        or node.compression
        or node.storage_name
        or node.storage not in {None, "\x00"}
    ):
        errors.append("Column collation, storage or foreign options unsupported")
    if node.identity not in {None, "\x00"} or node.generated not in {None, "\x00"}:
        errors.append("Identity/generated columns require separate sequence/expression analysis")
    details = {
        "type": render(node.typeName),
        "definition": render(node),
        "constraints": constraints,
        "not_null": bool(
            node.is_not_null or any(c["constraint_kind"] == "notnull" for c in constraints)
        ),
        "default": next(
            (c["default"] for c in constraints if c["constraint_kind"] == "default"), None
        ),
        "using": render(node.raw_default) if node.raw_default else None,
    }
    return details, errors


def parse_migration(sql: str) -> ParsedMigration:
    try:
        return _parse_migration(sql)
    except (RecursionError, UnicodeError):
        return ParsedMigration(
            valid=False,
            parser_version=PARSER_VERSION,
            diagnostics=[
                Diagnostic(
                    message="SQL extraction exceeded nesting limits or contains invalid Unicode"
                )
            ],
        )


def _parse_migration(sql: str) -> ParsedMigration:
    data = sql.encode("utf-8")
    if len(data) > MAX_SQL_BYTES or "\x00" in sql:
        return ParsedMigration(
            valid=False,
            parser_version=PARSER_VERSION,
            diagnostics=[Diagnostic(message="SQL exceeds byte limit or contains a null byte")],
        )
    try:
        statements = parse_sql(sql)
    except (pglast.parser.ParseError, RecursionError, ValueError) as error:
        character_offset = (
            error.args[1] if len(error.args) > 1 and isinstance(error.args[1], int) else 0
        )
        character_offset = max(0, min(len(sql), character_offset))
        prefix = sql[:character_offset]
        error_location = Location(
            statement=1,
            byte_offset=len(prefix.encode("utf-8")),
            byte_length=0,
            line=prefix.count("\n") + 1,
            column=len(prefix.rsplit("\n", 1)[-1]) + 1,
        )
        # pglast 8.5 remaps PostgreSQL's already character-based error cursor
        # through its byte displacement table. Unicode error cursors are thus
        # unreliable, although successfully parsed statement spans are exact.
        if not sql.isascii():
            error_location = None
        return ParsedMigration(
            valid=False,
            parser_version=PARSER_VERSION,
            diagnostics=[
                Diagnostic(message=f"PostgreSQL parse error: {error}", location=error_location)
            ],
        )
    parsed = ParsedMigration(parser_version=PARSER_VERSION)
    if not statements:
        parsed.valid = False
        parsed.diagnostics.append(Diagnostic(message="Migration contains no SQL statements"))
        return parsed
    for index, raw in enumerate(statements):
        character_start = raw.stmt_location
        character_end = character_start + raw.stmt_len if raw.stmt_len else len(sql)
        prefix = sql[:character_start]
        start = len(prefix.encode("utf-8"))
        end = len(sql[:character_end].encode("utf-8"))
        location = Location(
            statement=index + 1,
            byte_offset=start,
            byte_length=end - start,
            line=prefix.count("\n") + 1,
            column=len(prefix.rsplit("\n", 1)[-1]) + 1,
        )
        statement_sql = data[start:end].decode("utf-8").strip()
        node = raw.stmt

        def add(
            kind: str,
            *,
            table: str | None = None,
            column: str | None = None,
            details: dict | None = None,
            errors: list[str] | None = None,
            location: Location = location,
            statement_sql: str = statement_sql,
        ) -> None:
            details = dict(details or {})
            if errors:
                details["unsupported_reasons"] = sorted(set(errors))
                parsed.diagnostics.extend(
                    Diagnostic(message=error, location=location) for error in sorted(set(errors))
                )
            parsed.operations.append(
                Operation(
                    kind=kind,
                    table=table,
                    column=column,
                    details=details,
                    supported=not bool(errors),
                    location=location,
                    sql=statement_sql,
                )
            )

        if isinstance(node, ast.CreateStmt):
            errors = _relation_errors(node.relation)
            if (
                node.inhRelations
                or node.partbound
                or node.partspec
                or node.ofTypename
                or node.options
                or node.tablespacename
                or node.accessMethod
            ):
                errors.append(
                    "Inheritance, partitioning, typed tables, options, tablespace or access method unsupported"
                )
            if node.relation.relpersistence != "p" or node.oncommit.name != "ONCOMMIT_NOOP":
                errors.append("Temporary/unlogged tables and ON COMMIT unsupported")
            columns, constraints = [], []
            for element in node.tableElts or ():
                if isinstance(element, ast.ColumnDef):
                    details, unsupported = _column(element)
                    columns.append({"name": element.colname, **details})
                elif isinstance(element, ast.Constraint):
                    details, unsupported = _constraint(element)
                    constraints.append(details)
                else:
                    unsupported = [f"Unsupported table element: {type(element).__name__}"]
                errors.extend(unsupported)
            add(
                "create_table",
                table=relation_name(node.relation),
                details={
                    "columns": columns,
                    "constraints": constraints,
                    "if_not_exists": node.if_not_exists,
                },
                errors=errors,
            )
        elif isinstance(node, ast.AlterTableStmt):
            table = relation_name(node.relation)
            for command in node.cmds or ():
                subtype = command.subtype.name
                errors = _relation_errors(node.relation)
                details = {
                    "if_exists": command.missing_ok,
                    "cascade": command.behavior.name == "DROP_CASCADE",
                }
                if node.objtype.name != "OBJECT_TABLE" or not node.relation.inh:
                    errors.append("Only ordinary ALTER TABLE without ONLY is supported")
                if node.missing_ok:
                    errors.append("ALTER TABLE IF EXISTS can silently bypass schema preconditions")
                column = command.name
                if subtype == "AT_AddColumn":
                    kind = "add_column"
                    column = command.def_.colname
                    column_details, unsupported = _column(command.def_)
                    details.update(column_details)
                    errors.extend(unsupported)
                elif subtype == "AT_DropColumn":
                    kind = "drop_column"
                elif subtype == "AT_AlterColumnType":
                    kind = "alter_column_type"
                    column_details, unsupported = _column(command.def_)
                    details.update(column_details)
                    errors.extend(unsupported)
                elif subtype in {"AT_SetNotNull", "AT_DropNotNull"}:
                    kind = "set_not_null" if subtype == "AT_SetNotNull" else "drop_not_null"
                elif subtype == "AT_AddConstraint":
                    kind = "add_constraint"
                    constraint_details, unsupported = _constraint(command.def_)
                    details.update(constraint_details)
                    errors.extend(unsupported)
                elif subtype == "AT_ValidateConstraint":
                    kind = "validate_constraint"
                    details["name"] = command.name
                    column = None
                elif subtype == "AT_DropConstraint":
                    kind = "drop_constraint"
                    details["name"] = command.name
                    column = None
                else:
                    kind = "unsupported"
                    errors.append(f"Unsupported ALTER TABLE subcommand: {subtype}")
                add(kind, table=table, column=column, details=details, errors=errors)
        elif isinstance(node, ast.IndexStmt):
            errors = _relation_errors(node.relation) + _expression_errors(node.whereClause)
            for element in (node.indexParams or ()) + (node.indexIncludingParams or ()):
                errors.extend(_expression_errors(element.expr))
                if element.collation or element.opclass or element.opclassopts:
                    errors.append("Custom index collation/operator classes unsupported")
            if (
                node.accessMethod != "btree"
                or node.options
                or node.tableSpace
                or node.excludeOpNames
                or node.isconstraint
                or node.iswithoutoverlaps
            ):
                errors.append(
                    "Only ordinary btree indexes without storage options/tablespaces supported"
                )
            add(
                "create_index",
                table=relation_name(node.relation),
                details={
                    "name": node.idxname,
                    "concurrently": bool(node.concurrent),
                    "unique": bool(node.unique),
                    "columns": [element.name for element in node.indexParams or ()],
                    "definition": render(node),
                    "if_not_exists": bool(node.if_not_exists),
                },
                errors=errors,
            )
        elif isinstance(node, ast.DropStmt) and node.removeType.name in {
            "OBJECT_TABLE",
            "OBJECT_INDEX",
        }:
            for obj in node.objects:
                name = ".".join(quote_ident(part.sval) for part in obj)
                kind = "drop_table" if node.removeType.name == "OBJECT_TABLE" else "drop_index"
                add(
                    kind,
                    table=name if kind == "drop_table" else None,
                    details={
                        "name": name,
                        "cascade": node.behavior.name == "DROP_CASCADE",
                        "concurrently": bool(node.concurrent),
                        "if_exists": bool(node.missing_ok),
                    },
                )
        else:
            add("unsupported", errors=[f"Unsupported statement node: {type(node).__name__}"])
        if len(parsed.operations) > MAX_OPERATIONS:
            parsed.valid = False
            parsed.diagnostics.append(
                Diagnostic(message="Migration exceeds operation limit", location=location)
            )
            break
    return parsed
