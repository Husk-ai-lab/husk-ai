"""Conversion matrix: tokens bypassed -> equivalent USD on common LLM providers.

The benchmark runs on Groq (deliberately chosen because it is the cheapest
production-grade LLM provider). This makes the absolute USD figure unimpressive
in isolation. The honest narrative is:

  "We bypassed N tokens deterministically. On Groq this is $X. On the same
   architecture operating in production on Claude Sonnet 4 / GPT-4o, the
   equivalent saving per debug cycle would be $Y."

This script reads the same `DCS tokens saved` number that hero_report.py
produces, and renders the cross-provider conversion.

Usage:
    uv run python benchmark/cost_matrix.py --tokens 8_400_000
    uv run python benchmark/cost_matrix.py --from-db   # auto-detects from traces.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from husk_studio_shared.pricing import cost_usd

# Equal input/output token split assumption (conservative -- output is pricier
# and represents the work actually preserved).
DEFAULT_INPUT_FRACTION = 0.7

PROVIDERS = [
    ("groq:llama-3.1-8b-instant",     "llama-3.1-8b-instant"),
    ("groq:llama-3.3-70b-versatile",  "llama-3.3-70b-versatile"),
    ("openai:gpt-4o-mini",            "gpt-4o-mini"),
    ("openai:gpt-4o",                 "gpt-4o"),
    ("openai:gpt-4.1",                "gpt-4.1"),
    ("openai:o1",                     "o1"),
    ("anthropic:claude-haiku-3.5",    "claude-3-5-haiku"),
    ("anthropic:claude-sonnet-3.5",   "claude-3-5-sonnet"),
    ("anthropic:claude-sonnet-4",     "claude-sonnet-4"),
    ("anthropic:claude-opus-4",       "claude-opus-4"),
]


def _dcs_tokens_from_db(db: Path) -> int:
    con = sqlite3.connect(str(db))
    row = con.execute(
        """
        WITH fp AS (
            SELECT b.id AS branch_id, b.parent_run_id, b.fork_span_id,
                   fs.started_at AS fork_started_at
            FROM branches b
            JOIN spans fs ON fs.id = b.fork_span_id
        )
        SELECT COALESCE(SUM(s.tokens_in + s.tokens_out), 0)
        FROM fp
        JOIN spans s
          ON s.run_id = fp.parent_run_id
         AND s.started_at <= fp.fork_started_at
         AND s.kind = 'llm'
        """
    ).fetchone()
    con.close()
    return int(row[0] or 0)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tokens", type=int, default=None, help="Total tokens bypassed")
    p.add_argument("--from-db", action="store_true", help="Read tokens bypassed from ~/.husk/traces.db")
    p.add_argument("--db", type=Path, default=Path.home() / ".husk" / "traces.db")
    p.add_argument(
        "--input-fraction",
        type=float,
        default=DEFAULT_INPUT_FRACTION,
        help="Fraction of bypassed tokens that are input (default 0.7)",
    )
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    if args.from_db:
        if not args.db.exists():
            print(f"ERROR: {args.db} not found.", file=sys.stderr)
            return 2
        tokens = _dcs_tokens_from_db(args.db)
    elif args.tokens is not None:
        tokens = args.tokens
    else:
        print("Provide either --tokens N or --from-db.", file=sys.stderr)
        return 2

    if tokens <= 0:
        print(f"No tokens bypassed (n={tokens}). Run real_replays.py first.", file=sys.stderr)
        return 1

    tokens_in = int(tokens * args.input_fraction)
    tokens_out = tokens - tokens_in

    lines: list[str] = []

    def emit(line: str = "") -> None:
        lines.append(line)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))

    emit("# Husk benchmark -- cost equivalence matrix")
    emit("")
    emit(f"**Tokens bypassed deterministically (DCS):** {tokens:,}")
    emit(f"  - input tokens: {tokens_in:,} (fraction {args.input_fraction:.2f})")
    emit(f"  - output tokens: {tokens_out:,}")
    emit("")
    emit("These are tokens that the modify-and-replay engine bypassed at the")
    emit("upstream nodes of failed runs in the benchmark. The figure below")
    emit("translates that saving into per-provider USD using current list prices")
    emit("(packages/husk-studio-shared/src/husk_studio_shared/pricing.py).")
    emit("")
    emit("| Provider:model | $ saved (this benchmark) | vs Groq smallest |")
    emit("|---|---:|---:|")

    base = None
    for label, model in PROVIDERS:
        c = cost_usd(model, tokens_in, tokens_out)
        if c is None:
            emit(f"| {label} | (no rate) | -- |")
            continue
        if base is None:
            base = c if c > 0 else 1e-9
        ratio = c / base if base else 1.0
        emit(f"| {label} | ${c:,.4f} | {ratio:,.1f}x |")

    emit("")
    emit("**Reading the table.** The benchmark ran on Groq llama-3.x because")
    emit("it is the cheapest production LLM provider. The Groq saving is")
    emit("intentionally small in absolute dollars. The 'vs Groq smallest'")
    emit("column shows the multiplier you would observe if the same workload")
    emit("ran on each pricier provider. For a real customer running a debug")
    emit("cycle on Claude Sonnet 4, the saving per replay is the corresponding")
    emit("row, not the Groq baseline.")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
