"""Parse OTLP traces (JSON shape) into Husk Run/Span rows.

Supports OTel GenAI v1.36 semantic conventions with a defensive fallback to
older names (`gen_ai.usage.prompt_tokens` → `input_tokens`).

The input shape matches OTLP/HTTP JSON output (also what
`google.protobuf.json_format.MessageToDict` produces from the proto form).
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# ── secret redaction ─────────────────────────────────────────────────────────
# Recorded prompts/completions/tool I/O are persisted in cleartext in traces.db
# and may also be shipped to the BYOK debugger LLM. Scrub the obvious provider
# secret shapes before persist. Opt out with HUSK_NO_REDACT=1. This is a coarse
# net (not a guarantee) — it catches the common key/token formats, not arbitrary
# user PII.
_REDACTED = "***REDACTED***"
_TOKEN_PATTERNS = [
    # One `sk-` rule for every provider that uses the prefix. The character class
    # MUST include `-` and `_`: OpenRouter keys are `sk-or-v1-…` and OpenAI project
    # keys are `sk-proj-…`, so an alphanumeric-only class stops at the first hyphen
    # and leaves the key in the clear. This also subsumes Anthropic's `sk-ant-…`.
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),  # Google API key
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"),  # GitHub classic PAT / OAuth / refresh
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),  # Authorization: Bearer ...
]
_KEYED = re.compile(
    r'(?i)(api[_-]?key|secret|token|password)("?\s*[:=]\s*"?)([A-Za-z0-9._\-]{8,})'
)


def _redact_text(s: str) -> str:
    out = _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", s)
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def _redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    return obj


def _redaction_disabled() -> bool:
    return os.environ.get("HUSK_NO_REDACT", "").strip().lower() in {"1", "true", "yes", "on"}


def _maybe_redact(obj: Any) -> Any:
    if _redaction_disabled():
        return obj
    return _redact(obj)


def _redact_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Redact span attributes, leaving Husk's own control attributes verbatim.

    Attributes are persisted, served over the API and WebSocket, and included in
    `husk-studio export` — the bundle we tell people to attach to bug reports — so they
    need the same scrub as prompts and completions. The `husk.` namespace is exempt
    because Husk sets those values itself and reads them back operationally
    (`husk.graph_module` is a filesystem path that replay must import); scrubbing a
    path that happened to contain e.g. `secret=` would silently break replay.
    """
    if _redaction_disabled():
        return attrs
    return {k: (v if k.startswith("husk.") else _redact(v)) for k, v in attrs.items()}


def decode_attr_value(v: dict[str, Any]) -> Any:
    """Decode an OTLP AnyValue dict into a native Python value."""
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [decode_attr_value(x) for x in v["arrayValue"].get("values") or []]
    if "kvlistValue" in v:
        return decode_attributes(v["kvlistValue"].get("values") or [])
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def decode_attributes(attrs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Decode an OTLP attributes list into a flat {key: value} dict."""
    out: dict[str, Any] = {}
    for a in attrs or []:
        k = a.get("key")
        if k is None:
            continue
        out[k] = decode_attr_value(a.get("value") or {})
    return out


def _nano_to_ms(n: Any) -> int:
    if n is None:
        return 0
    try:
        return int(n) // 1_000_000
    except (TypeError, ValueError):
        return 0


_HUSK_KINDS = {"llm", "tool", "chain", "agent_decision", "graph_node", "state_set"}


def _resolve_kind(span_attrs: dict[str, Any]) -> str:
    """Heuristic mapping from GenAI span attributes to Husk span kinds.

    An explicit `husk.span_kind` (set by Husk's own instrumentation, e.g. the
    engine telemetry hook's per-node `graph_node` spans) wins over the heuristics.
    Otherwise: `gen_ai.system` alone is not enough (root agent spans often set
    just the system tag for context), so we require `gen_ai.operation.name` to
    classify as `llm`.
    """
    explicit = span_attrs.get("husk.span_kind")
    if isinstance(explicit, str) and explicit in _HUSK_KINDS:
        return explicit
    if span_attrs.get("gen_ai.tool.name") or span_attrs.get("gen_ai.tool.type"):
        return "tool"
    if "gen_ai.operation.name" in span_attrs:
        return "llm"
    return "chain"


def _tokens(attrs: dict[str, Any], side: str) -> int | None:
    # v1.36: gen_ai.usage.input_tokens / output_tokens
    # legacy: gen_ai.usage.prompt_tokens / completion_tokens
    v = attrs.get(f"gen_ai.usage.{side}_tokens")
    if v is None:
        legacy = {"input": "prompt", "output": "completion"}[side]
        v = attrs.get(f"gen_ai.usage.{legacy}_tokens")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_id(s: str) -> str:
    """Normalize an OTLP trace/span ID to hex (lowercase).

    The JSON wire form represents bytes as hex; the proto wire form (after
    `MessageToDict`) represents them as base64. We canonicalize so URLs and
    DB keys are stable regardless of input format.
    """
    if not s:
        return ""
    s = s.replace("-", "")
    if all(c in "0123456789abcdefABCDEF" for c in s):
        return s.lower()
    # Treat as base64 — pad if needed before decoding.
    try:
        padded = s + "=" * ((-len(s)) % 4)
        return base64.b64decode(padded).hex()
    except Exception:  # noqa: BLE001
        return s


def _trace_id_to_run_id(trace_id: str) -> str:
    # Stable + idempotent: first 26 hex chars (~104 bits of entropy).
    return _normalize_id(trace_id)[:26]


def _span_id_short(span_id: str) -> str:
    return _normalize_id(span_id)[:26]


def _extract_messages(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split GenAI span events into input messages and output choices."""
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for ev in events or []:
        name = ev.get("name") or ""
        attrs = decode_attributes(ev.get("attributes") or [])
        if name.startswith("gen_ai.") and name.endswith(".message"):
            # e.g. gen_ai.user.message, gen_ai.assistant.message, gen_ai.tool.message
            role = name.removeprefix("gen_ai.").removesuffix(".message")
            inputs.append({"role": role, **attrs})
        elif name == "gen_ai.choice":
            outputs.append(attrs)
    return inputs, outputs


@dataclass
class ParsedSpan:
    run_id: str
    id: str
    parent_span_id: str | None
    kind: str
    name: str
    started_at_ms: int
    finished_at_ms: int | None
    status: str
    input_inline: Any
    output_inline: Any
    tokens_in: int | None
    tokens_out: int | None
    provider: str | None
    model: str | None
    attrs: dict[str, Any]
    error_payload: dict[str, Any] | None
    service_name: str | None
    gen_ai_system: str | None


def parse_otlp_traces(body: dict[str, Any]) -> list[ParsedSpan]:
    """Walk OTLP `resourceSpans` → `scopeSpans` → `spans` and yield ParsedSpans."""
    out: list[ParsedSpan] = []
    for rs in body.get("resourceSpans") or []:
        resource_attrs = decode_attributes(
            (rs.get("resource") or {}).get("attributes") or []
        )
        service_name = resource_attrs.get("service.name")
        for ss in rs.get("scopeSpans") or []:
            for span in ss.get("spans") or []:
                span_attrs = decode_attributes(span.get("attributes") or [])
                inputs, outputs = _extract_messages(span.get("events") or [])
                trace_id = span.get("traceId") or ""
                span_id = span.get("spanId") or ""
                parent_span = span.get("parentSpanId") or None

                status_block = span.get("status") or {}
                status_code = status_block.get("code")
                is_error = status_code == 2 or status_code == "STATUS_CODE_ERROR"
                status = "error" if is_error else "success"
                err_payload = (
                    {"message": status_block.get("message")} if is_error else None
                )

                out.append(
                    ParsedSpan(
                        run_id=_trace_id_to_run_id(trace_id),
                        id=_span_id_short(span_id),
                        parent_span_id=(
                            _span_id_short(parent_span) if parent_span else None
                        ),
                        kind=_resolve_kind(span_attrs),
                        name=span.get("name") or "",
                        started_at_ms=_nano_to_ms(span.get("startTimeUnixNano")),
                        finished_at_ms=_nano_to_ms(span.get("endTimeUnixNano")) or None,
                        status=status,
                        input_inline=_maybe_redact({"messages": inputs}) if inputs else None,
                        output_inline=_maybe_redact({"choices": outputs}) if outputs else None,
                        tokens_in=_tokens(span_attrs, "input"),
                        tokens_out=_tokens(span_attrs, "output"),
                        provider=span_attrs.get("gen_ai.system"),
                        model=(
                            span_attrs.get("gen_ai.response.model")
                            or span_attrs.get("gen_ai.request.model")
                        ),
                        attrs={**_redact_attrs(span_attrs), "_resource": resource_attrs},
                        error_payload=_maybe_redact(err_payload),
                        service_name=service_name,
                        gen_ai_system=span_attrs.get("gen_ai.system"),
                    )
                )
    return out
