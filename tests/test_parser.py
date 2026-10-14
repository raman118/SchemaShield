import pytest
from schemashield.parser import MAX_SQL_BYTES, parse_migration


@pytest.mark.parametrize(
    "sql,kinds",
    [
        ('CREATE TABLE "Üsers" ("eMail" text, id bigint PRIMARY KEY)', ["create_table"]),
        (
            "ALTER TABLE public.t ADD COLUMN x int, DROP COLUMN y, ALTER COLUMN z TYPE text",
            ["add_column", "drop_column", "alter_column_type"],
        ),
        (
            "ALTER TABLE t ALTER COLUMN x SET NOT NULL; ALTER TABLE t ALTER COLUMN x DROP NOT NULL",
            ["set_not_null", "drop_not_null"],
        ),
        (
            "CREATE UNIQUE INDEX CONCURRENTLY ix ON t(x) WHERE x > 0; DROP INDEX CONCURRENTLY ix",
            ["create_index", "drop_index"],
        ),
        (
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY(x) REFERENCES parent(id) NOT VALID; ALTER TABLE t VALIDATE CONSTRAINT fk",
            ["add_constraint", "validate_constraint"],
        ),
        ("ALTER TABLE t ADD CONSTRAINT ck CHECK(x IS NOT NULL) NOT VALID", ["add_constraint"]),
        ("DROP TABLE IF EXISTS public.t, u CASCADE", ["drop_table", "drop_table"]),
        ("ALTER TABLE t ADD COLUMN d numeric(12,2) DEFAULT 2", ["add_column"]),
        ("CREATE TABLE t(v int[], title character varying(20))", ["create_table"]),
        ("ALTER TABLE t ALTER COLUMN x TYPE bigint USING x::bigint", ["alter_column_type"]),
    ],
)
def test_supported(sql, kinds):
    result = parse_migration(sql)
    assert result.valid, result.diagnostics
    assert [op.kind for op in result.operations] == kinds
    assert all(op.supported for op in result.operations), result.diagnostics


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "DO $$ BEGIN RAISE NOTICE 'hello'; END $$",
        "COPY t FROM PROGRAM 'whoami'",
        "ALTER TABLE t ENABLE TRIGGER ALL",
        "ALTER TABLE t ADD COLUMN a int, OWNER TO evil",
        "CREATE TABLE t AS SELECT 1",
        "CREATE TABLE t (LIKE parent INCLUDING ALL)",
        "CREATE TABLE t (id int) PARTITION BY RANGE(id)",
        "CREATE TABLE t(id int) INHERITS(parent)",
        "CREATE TABLE t (x serial)",
        "ALTER TABLE t ADD COLUMN x int GENERATED ALWAYS AS IDENTITY",
        "ALTER TABLE t ADD COLUMN x int DEFAULT pg_sleep(1)",
        "ALTER TABLE t ADD COLUMN x timestamp DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE t ALTER COLUMN x TYPE text USING evil(x)",
        "ALTER TABLE t ADD CONSTRAINT ck CHECK(evil(x))",
        "CREATE INDEX ix ON t(lower(x))",
        "CREATE INDEX ix ON t(x) WHERE evil(x)",
        "CREATE INDEX ix ON t USING gin(x)",
        "ALTER TABLE t ADD COLUMN x evil.custom",
        "ALTER TABLE ONLY t ADD COLUMN x int",
        'CREATE TABLE t (x text COLLATE "C")',
        "CREATE TABLE t(x int GENERATED ALWAYS AS (1) STORED)",
    ],
)
def test_unknown_child_syntax_fails_closed(sql):
    result = parse_migration(sql)
    assert result.valid
    assert any(not op.supported for op in result.operations)
    assert result.diagnostics


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- only comments",
        "ALTER TABL t",
        "CREATE TABLE (",
        "SELECT 'unterminated",
        "SELECT '\x00'",
    ],
)
def test_invalid(sql):
    result = parse_migration(sql)
    assert not result.valid
    assert result.diagnostics


def test_unicode_locations_and_original_source_spans():
    sql = 'CREATE TABLE "é表"(x int);\n-- comments\nALTER TABLE "é表" ADD COLUMN "名" text;'
    result = parse_migration(sql)
    data = sql.encode()
    assert len(result.operations) == 2
    for op in result.operations:
        loc = op.location
        assert data[loc.byte_offset : loc.byte_offset + loc.byte_length].decode().strip() == op.sql
    assert result.operations[1].location.byte_offset > sql.index(";")
    assert result.operations[1].table == '"é表"'
    assert result.operations[1].column == "名"


def test_semicolons_in_literals_do_not_split_statements():
    result = parse_migration(
        "CREATE TABLE t (x text DEFAULT 'a;b'); /* ; */ ALTER TABLE t ADD COLUMN y int;"
    )
    assert len(result.operations) == 2
    assert all(op.supported for op in result.operations)


def test_size_limit():
    assert not parse_migration(" " * (MAX_SQL_BYTES + 1)).valid


def test_all_subcommands_are_visible():
    parsed = parse_migration("ALTER TABLE t ADD COLUMN x int, DROP COLUMN y, ENABLE TRIGGER ALL")
    assert len(parsed.operations) == 3
    assert not parsed.operations[-1].supported


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE t ADD CONSTRAINT ck CHECK(x>0) NOT ENFORCED",
        "CREATE TABLE t(x text COMPRESSION pglz)",
        "ALTER TABLE IF EXISTS t ADD COLUMN x int",
    ],
)
def test_unsupported_version_or_modifier(sql):
    result = parse_migration(sql)
    assert not result.operations[0].supported


def test_error_location_accounts_for_unicode():
    sql = 'CREATE TABLE "表"(x int);\nALTER TABL t'
    result = parse_migration(sql)
    assert not result.valid
    assert result.diagnostics[0].location is None
    assert "TABL" in result.diagnostics[0].message


def test_deep_expression_rejects_without_uncaught_recursion():
    sql = "CREATE TABLE t(x int DEFAULT " + "1+" * 600 + "1)"
    parsed = parse_migration(sql)
    assert not parsed.valid or not parsed.operations[0].supported


def test_invalid_unicode_does_not_crash_parser():
    assert not parse_migration("CREATE TABLE t(x text DEFAULT '\ud800')").valid


@pytest.mark.parametrize(
    "sql", ["CREATE TABLE t(x text STORAGE EXTERNAL)", "ALTER TABLE t ADD COLUMN x numeric(1+1)"]
)
def test_storage_and_nonliteral_type_modifiers_fail_closed(sql):
    result = parse_migration(sql)
    assert result.valid
    assert not result.operations[0].supported


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE otherdb.public.accounts ADD COLUMN x text",
        "CREATE TABLE otherdb.public.new_table (id bigint)",
        "CREATE INDEX CONCURRENTLY i ON otherdb.public.accounts(email)",
        "CREATE TABLE new_table (id bigint REFERENCES otherdb.public.accounts(id))",
        "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY(account_id) REFERENCES otherdb.public.accounts(id) NOT VALID",
    ],
)
def test_database_qualified_relations_are_preserved_and_fail_closed(sql):
    result = parse_migration(sql)
    assert result.valid
    assert not result.operations[0].supported
    assert any("Database-qualified" in d.message for d in result.diagnostics)
    operation = result.operations[0]
    if "REFERENCES" not in sql:
        assert operation.table.startswith('"otherdb"."public".')
    elif operation.kind == "add_constraint":
        assert operation.details["references"] == '"otherdb"."public"."accounts"'


def test_foreign_key_actions_are_structured_evidence():
    result = parse_migration(
        "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE ON UPDATE SET NULL NOT VALID"
    )
    assert result.operations[0].supported
    assert result.operations[0].details["on_delete"] == "cascade"
    assert result.operations[0].details["on_update"] == "set_null"
