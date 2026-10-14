"""Immutable API artifacts shared by parser, policy, planner and UI."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Location(Artifact):
    statement: int
    byte_offset: int
    byte_length: int
    line: int
    column: int


class Operation(Artifact):
    kind: str
    table: str | None = None
    column: str | None = None
    location: Location
    details: dict[str, Any] = Field(default_factory=dict)
    sql: str
    supported: bool = True


class Diagnostic(Artifact):
    message: str
    location: Location | None = None


class ParsedMigration(Artifact):
    operations: list[Operation] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    valid: bool = True
    parser_version: str


class Finding(Artifact):
    rule_id: str
    rule_version: str = "1.0"
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str
    location: Location
    explanation: str
    evidence: dict[str, Any]
    mitigation: str


class Contribution(Artifact):
    category: str
    deduction: int
    reason: str


class SafetyScore(Artifact):
    value: int = Field(ge=0, le=100)
    higher_is_safer: bool = True
    policy_version: str = "score-1.0"
    contributions: list[Contribution]
    explanation: str


class Analysis(Artifact):
    artifact_hash: str
    policy_version: str = "rules-1.0"
    postgres_version: str = "17"
    parsed: ParsedMigration
    findings: list[Finding]
    score: SafetyScore
    decision: Literal["blocked", "review_required"]
    tested: bool = False
    assumptions: list[str]


class PlanStep(Artifact):
    id: str
    phase: Literal["EXPAND", "BACKFILL", "VERIFY", "CONTRACT"]
    description: str
    sql: str | None = None
    transaction: Literal["transactional", "autocommit", "manual"] = "transactional"
    requires_approval: bool = False
    preconditions: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)


class Plan(Artifact):
    eligible: bool
    reason: str
    artifact_hash: str
    steps: list[PlanStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SubmitMigration(Artifact):
    title: str = Field(min_length=1, max_length=120)
    sql: str = Field(min_length=1, max_length=100_000)

    @field_validator("sql")
    @classmethod
    def utf8_sql(cls, value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("SQL must be valid UTF-8 Unicode") from exc
        if "\x00" in value:
            raise ValueError("SQL cannot contain NUL characters")
        return value


class ApprovalRequest(Artifact):
    artifact_hash: str = Field(min_length=64, max_length=64)
    report_id: str
    reviewer: str = Field(min_length=1, max_length=100)


class APIError(Artifact):
    code: str
    message: str
    request_id: str
