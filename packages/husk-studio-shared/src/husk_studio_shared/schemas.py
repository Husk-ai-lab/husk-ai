from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from husk_studio_shared.enums import Framework, OverrideType, RunStatus, SpanKind, SpanStatus


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Run(_Base):
    id: str
    parent_run_id: str | None = None
    fork_span_id: str | None = None
    script_path: str
    script_argv: list[str] = Field(default_factory=list)
    framework: Framework = Framework.UNKNOWN
    status: RunStatus = RunStatus.PENDING
    started_at: int
    finished_at: int | None = None
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    env_fingerprint: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Span(_Base):
    id: str
    run_id: str
    parent_span_id: str | None = None
    kind: SpanKind
    name: str
    started_at: int
    finished_at: int | None = None
    status: SpanStatus = SpanStatus.RUNNING
    input_ref: str | None = None
    input_inline: Any | None = None
    output_ref: str | None = None
    output_inline: Any | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    provider: str | None = None
    model: str | None = None
    http_request_ref: str | None = None
    error_payload: dict[str, Any] | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class Snapshot(_Base):
    id: str
    run_id: str
    span_id: str
    created_at: int
    state_ref: str
    state_schema_version: int = 1
    rng_state: dict[str, Any] | None = None
    http_cassette_ref: str | None = None
    size_bytes: int = 0
    serializer: str = "pydantic_v0"


class Branch(_Base):
    id: str
    parent_run_id: str
    child_run_id: str
    fork_span_id: str
    override_payload: dict[str, Any]
    override_type: OverrideType
    created_at: int
    label: str | None = None
    notes: str | None = None


class SpanEvent(_Base):
    """JSONL event emitted by the sandbox tracer over FD3 to the backend ingest."""

    type: str  # "span.start" | "span.end" | "span.update" | "run.start" | "run.end"
    run_id: str
    span_id: str | None = None
    ts: int
    data: dict[str, Any] = Field(default_factory=dict)
