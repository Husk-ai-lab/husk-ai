"""Pitch-ready markdown summary of Husk benchmark hero metrics, extracted
live from ~/.husk/traces.db with BCa Bootstrap 95% confidence intervals.

5 hero metrics + supporting infrastructure footnotes.

PRIMARY HERO (the slide-6 numbers):
  D1 -- Replay Wall-Time Speed-up
        How many times faster a replay is vs the parent's full run.
  D2 -- Token Bypass Rate per failure mode
        Bypass varies by where the failure occurred: late in the graph
        (N4) bypasses more tokens than early (N1).
  D3 -- MAX Token Bypass Observed
        Single best-case observation across all replays.
  D4 -- Replay Success Rate
        % of replays that produced a valid child run.
  D5 -- Mean Token Bypass Rate
        Aggregate baseline across all replays.

SUPPORTING (slide-7 footnote):
  Husk Ingest Overhead (ms)   -- vs Langfuse 0.1ms, LangSmith 132ms
  Storage Efficiency           -- bytes / trace, vs Datadog $0.10/GB

All numbers derive from SQL queries on the live SQLite DB. Bootstrap BCa
implementation in benchmark/bootstrap.py, self-validated against the
Efron-Tibshirani 1993 GPA example.

Usage:
    uv run python benchmark/hero_report.py [--db PATH] [--since TS] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Make `benchmark.bootstrap` importable when this file is invoked as a script.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark.bootstrap import bca_ci, wilson_ci  # noqa: E402
from husk_studio_shared.pricing import cost_usd  # noqa: E402

DB_PATH = Path.home() / ".husk" / "traces.db"


def _is_real_llm(con: sqlite3.Connection, run_id: str) -> bool:
    row = con.execute(
        "SELECT json_extract(s.attrs, '$.\"husk.benchmark.real_llm\"') "
        "FROM spans s WHERE s.run_id = ? AND s.name = 'agent.run' LIMIT 1",
        (run_id,),
    ).fetchone()
    if not row:
        return False
    v = row[0]
    if v is None:
        return False
    return bool(v) and str(v).lower() not in ("false", "0")


def _percentiles(values: list[float], qs: list[float]) -> list[float]:
    if not values:
        return [0.0] * len(qs)
    s = sorted(values)
    out = []
    for q in qs:
        idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
        out.append(s[idx])
    return out


def collect(db_path: Path, since: int = 0) -> dict:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # ── Volume & failure rate
    # Replay children are recorded as fresh runs with a NULL parent_run_id (the
    # parent↔child link lives in the `branches` table), so a naive
    # "parent_run_id IS NULL" count double-counts them (the paper's n=617
    # "counting quirk"). Exclude any run that is a branch child to get the true
    # parent population.
    child_ids = {row[0] for row in con.execute("SELECT child_run_id FROM branches")}
    all_roots = con.execute(
        "SELECT id, status, started_at, finished_at, total_tokens_in, "
        "total_tokens_out, total_cost_usd FROM runs "
        "WHERE parent_run_id IS NULL AND started_at >= ?",
        (since,),
    ).fetchall()
    parents = [p for p in all_roots if p["id"] not in child_ids]
    n_runs = con.execute(
        "SELECT COUNT(*) FROM runs WHERE started_at >= ?", (since,)
    ).fetchone()[0]
    n_parents = len(parents)
    n_failed = sum(1 for p in parents if p["status"] in ("error", "aborted"))
    failure_rate = (100.0 * n_failed / n_parents) if n_parents else 0.0
    real_llm_parents = sum(1 for p in parents if _is_real_llm(con, p["id"]))

    breakdown = con.execute(
        "SELECT json_extract(s.attrs, '$.\"benchmark.failure_mode\"') AS mode, "
        "COUNT(DISTINCT s.run_id) AS n "
        "FROM spans s JOIN runs r ON r.id = s.run_id "
        "WHERE r.status = 'error' AND r.parent_run_id IS NULL "
        "  AND r.started_at >= ? "
        "  AND json_extract(s.attrs, '$.\"benchmark.failure_mode\"') IS NOT NULL "
        "GROUP BY mode ORDER BY n DESC",
        (since,),
    ).fetchall()

    # ── Branch-level data we'll need for D1-D5
    # Pull every branch joined with parent + child runs + parent failure_mode.
    rows = con.execute(
        """
        WITH parent_info AS (
            SELECT r.id AS pid, r.started_at AS pstart, r.finished_at AS pend
            FROM runs r
            WHERE r.parent_run_id IS NULL AND r.started_at >= ?
        ),
        parent_mode AS (
            SELECT s.run_id AS pid,
                   json_extract(s.attrs, '$."benchmark.failure_mode"') AS mode
            FROM spans s
            WHERE s.name = 'agent.run'
              AND json_extract(s.attrs, '$."benchmark.failure_mode"') IS NOT NULL
        ),
        parent_tokens AS (
            SELECT b.id AS branch_id, b.parent_run_id, b.child_run_id,
                   SUM(s.tokens_in + s.tokens_out) AS parent_t
            FROM branches b
            JOIN spans s ON s.run_id = b.parent_run_id AND s.kind = 'llm'
            JOIN parent_info pi ON pi.pid = b.parent_run_id
            GROUP BY b.id
        ),
        child_tokens AS (
            SELECT b.id AS branch_id,
                   SUM(s.tokens_in + s.tokens_out) AS child_t
            FROM branches b
            JOIN spans s ON s.run_id = b.child_run_id AND s.kind = 'llm'
            GROUP BY b.id
        ),
        child_info AS (
            SELECT b.id AS branch_id,
                   cr.started_at AS cstart,
                   cr.finished_at AS cend
            FROM branches b
            JOIN runs cr ON cr.id = b.child_run_id
        )
        SELECT
            pt.branch_id,
            pt.parent_run_id,
            pt.child_run_id,
            COALESCE(pt.parent_t, 0) AS parent_t,
            COALESCE(ct.child_t, 0)  AS child_t,
            pi.pstart, pi.pend,
            ci.cstart, ci.cend,
            pm.mode AS failure_mode
        FROM parent_tokens pt
        LEFT JOIN child_tokens ct ON ct.branch_id = pt.branch_id
        LEFT JOIN parent_info pi ON pi.pid = pt.parent_run_id
        LEFT JOIN child_info ci ON ci.branch_id = pt.branch_id
        LEFT JOIN parent_mode pm ON pm.pid = pt.parent_run_id
        """,
        (since,),
    ).fetchall()

    # D5 (mean) + per-branch bypass rates
    bypass_rates: list[float] = []
    bypassed_tokens_total = 0
    parent_tokens_total = 0
    # D2 by failure mode
    by_mode: dict[str, list[float]] = {}
    # D1 wall-time speedup
    speedups: list[float] = []
    # D4 replay success
    n_total_branches = len(rows)
    n_branches_with_child = 0

    for r in rows:
        pt = r["parent_t"] or 0
        ct = r["child_t"] or 0
        if pt and pt > 0:
            saved = max(0, pt - ct)
            rate = 100.0 * saved / pt
            bypass_rates.append(rate)
            bypassed_tokens_total += saved
            parent_tokens_total += pt
            mode = r["failure_mode"] or "unknown"
            by_mode.setdefault(mode, []).append(rate)

        # D1 wall-time
        if r["pstart"] is not None and r["pend"] is not None and r["cstart"] is not None and r["cend"] is not None:
            parent_dur = max(1, r["pend"] - r["pstart"])
            child_dur = max(1, r["cend"] - r["cstart"])
            if child_dur > 0:
                speedups.append(parent_dur / child_dur)

        if r["child_run_id"]:
            n_branches_with_child += 1

    # ── D1: Wall-time speed-up
    d1 = {"n": len(speedups)}
    if speedups:
        # Sort before bootstrapping so the (seeded) resampling is independent of
        # DB row order — makes the metric byte-reproducible from any DB or
        # fixture holding the same data. Point estimate/percentiles are unaffected.
        mean, lo, hi = bca_ci(sorted(speedups), n_resamples=10_000)
        p50, p95 = _percentiles(speedups, [0.5, 0.95])
        d1.update(
            mean_x=round(mean, 2),
            ci95_low_x=round(lo, 2),
            ci95_high_x=round(hi, 2),
            p50_x=round(p50, 2),
            p95_x=round(p95, 2),
            max_x=round(max(speedups), 2),
        )

    # ── D2: Bypass rate per failure mode
    d2: dict[str, dict] = {}
    for mode in sorted(by_mode):
        rates = by_mode[mode]
        item = {"n": len(rates)}
        if rates:
            if len(rates) >= 2:
                m, lo, hi = bca_ci(sorted(rates), n_resamples=10_000)
                item.update(
                    mean_pct=round(m, 2),
                    ci95_low_pct=round(lo, 2),
                    ci95_high_pct=round(hi, 2),
                    max_pct=round(max(rates), 2),
                )
            else:
                item.update(
                    mean_pct=round(rates[0], 2),
                    ci95_low_pct=round(rates[0], 2),
                    ci95_high_pct=round(rates[0], 2),
                    max_pct=round(rates[0], 2),
                )
        d2[mode] = item

    # ── D3: MAX token bypass observed
    d3 = {
        "max_pct": round(max(bypass_rates), 2) if bypass_rates else 0.0,
        "n_branches": len(bypass_rates),
    }

    # ── D4: Replay success rate (Wilson score interval — robust at p≈1 / small n)
    d4 = {
        "n_total_branches": n_total_branches,
        "n_with_child": n_branches_with_child,
        "success_pct": (
            round(100.0 * n_branches_with_child / n_total_branches, 2)
            if n_total_branches else 0.0
        ),
    }
    if n_total_branches:
        _p, _lo, _hi = wilson_ci(n_branches_with_child, n_total_branches)
        d4.update(
            ci95_low_pct=round(100.0 * _lo, 2),
            ci95_high_pct=round(100.0 * _hi, 2),
        )

    # ── D5: Mean bypass rate (aggregate baseline)
    d5 = {
        "n_branches": len(bypass_rates),
        "tokens_bypassed_total": int(bypassed_tokens_total),
        "tokens_parent_total": int(parent_tokens_total),
    }
    if bypass_rates:
        m, lo, hi = bca_ci(sorted(bypass_rates), n_resamples=10_000)
        d5.update(
            mean_pct=round(m, 2),
            ci95_low_pct=round(lo, 2),
            ci95_high_pct=round(hi, 2),
        )

    # ── SUPPORTING: ingest overhead + storage
    overhead_samples: list[float] = []
    proxy = con.execute(
        """
        SELECT MIN(s.started_at) - r.started_at AS lag_ms
        FROM runs r
        JOIN spans s ON s.run_id = r.id AND s.name <> 'agent.run'
        WHERE r.parent_run_id IS NULL AND r.started_at >= ?
        GROUP BY r.id
        """,
        (since,),
    ).fetchall()
    for row in proxy:
        v = row[0]
        if v is not None and 0 <= v < 5000:
            overhead_samples.append(float(v))
    overhead = {"n": len(overhead_samples)}
    if overhead_samples:
        m, lo, hi = bca_ci(sorted(overhead_samples), n_resamples=10_000)
        p50, p95 = _percentiles(overhead_samples, [0.5, 0.95])
        overhead.update(
            mean_ms=round(m, 2),
            ci95_low_ms=round(lo, 2),
            ci95_high_ms=round(hi, 2),
            p50_ms=round(p50, 2),
            p95_ms=round(p95, 2),
        )

    db_size = db_path.stat().st_size
    storage = {
        "db_size_bytes": db_size,
        "db_size_mb": round(db_size / (1024 * 1024), 3),
        # Every recorded run is a stored trace (parents AND replay children).
        "bytes_per_trace": round(db_size / max(n_runs, 1), 1),
    }

    # Cost & token totals computed from the authoritative per-span token counts
    # via the shared pricing table — the runs.total_cost_usd rollup can be stale
    # when a model's price was added after ingest (exactly what happened for the
    # OpenRouter model IDs in the canonical run, which left the rollup at $0).
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for s in con.execute(
        "SELECT s.model AS model, s.tokens_in AS ti, s.tokens_out AS to_ FROM spans s "
        "JOIN runs r ON r.id = s.run_id "
        "WHERE s.kind = 'llm' AND r.started_at >= ?",
        (since,),
    ):
        ti = s["ti"] or 0
        to = s["to_"] or 0
        total_in += ti
        total_out += to
        c = cost_usd(s["model"], ti, to)
        if c:
            total_cost += c

    con.close()
    return {
        "n_runs": n_runs,
        "n_parents": n_parents,
        "n_failed": n_failed,
        "failure_rate_pct": round(failure_rate, 2),
        "real_llm_runs": real_llm_parents,
        "failure_breakdown": [(m, n) for m, n in breakdown],
        "total_cost_usd": round(total_cost, 4),
        "total_tokens_in": total_in,
        "total_tokens_out": total_out,
        "d1_wall_time_speedup": d1,
        "d2_bypass_by_failure_mode": d2,
        "d3_max_bypass": d3,
        "d4_replay_success": d4,
        "d5_mean_bypass": d5,
        "supporting_ingest_overhead_ms": overhead,
        "supporting_storage_efficiency": storage,
    }


def render(data: dict, db_path: Path, since: int) -> str:
    lines: list[str] = []
    lines.append("# Husk benchmark -- hero metrics (OpenRouter Llama + Bootstrap BCa)")
    lines.append("")
    lines.append(f"- Source: `{db_path}` (live SQLite)")
    lines.append(f"- Filter: parent runs with `started_at >= {since}`")
    lines.append("- CI methodology: BCa Bootstrap, 10,000 resamples (benchmark/bootstrap.py)")
    lines.append("- Self-validated against Efron-Tibshirani 1993 GPA example.")
    lines.append("")
    lines.append("## Volume")
    lines.append("")
    lines.append(
        f"- Parent runs in this benchmark: **{data['n_parents']:,}** "
        f"(+ {data.get('n_runs', data['n_parents']) - data['n_parents']:,} replay children "
        f"= {data.get('n_runs', data['n_parents']):,} total recorded traces)"
    )
    lines.append(f"  - with real LLM calls: **{data['real_llm_runs']:,}**")
    lines.append(f"- Failed parent runs: **{data['n_failed']:,}** ({data['failure_rate_pct']}%)")
    lines.append(
        f"- LLM spend (list-price estimate over recorded tokens): "
        f"**${data['total_cost_usd']:,.4f}**"
    )
    lines.append(
        f"- Token consumption: {data['total_tokens_in']:,} in / {data['total_tokens_out']:,} out"
    )
    lines.append("")
    if data["failure_breakdown"]:
        lines.append("Failure breakdown by injected mode:")
        for mode, n in data["failure_breakdown"]:
            lines.append(f"  - {mode}: {n:,}")
        lines.append("")

    # ── D1
    d1 = data["d1_wall_time_speedup"]
    lines.append("## D1 -- Replay Wall-Time Speed-up (PRIMARY HERO)")
    lines.append("")
    if d1.get("n", 0) == 0:
        lines.append("_(no replay pairs measured yet)_")
    else:
        lines.append(
            f"**{d1['mean_x']}x faster** -- mean wall-time of a replay vs the parent's full run "
            f"(95% CI [{d1['ci95_low_x']}x, {d1['ci95_high_x']}x], n={d1['n']})"
        )
        lines.append(f"- p50: {d1['p50_x']}x   p95: {d1['p95_x']}x   max: {d1['max_x']}x")
    lines.append("")
    lines.append("**Why this matters:** every replay short-circuits the upstream nodes.")
    lines.append("The wall-clock saving compounds with every iteration in a debug cycle.")
    lines.append("")

    # ── D2
    d2 = data["d2_bypass_by_failure_mode"]
    lines.append("## D2 -- Token Bypass Rate per failure mode")
    lines.append("")
    if not d2:
        lines.append("_(no per-mode samples)_")
    else:
        lines.append("| Failure mode | Mean bypass | 95% CI | Max | n |")
        lines.append("|---|---:|---:|---:|---:|")
        for mode in sorted(d2):
            item = d2[mode]
            n = item.get("n", 0)
            if n == 0:
                lines.append(f"| {mode} | -- | -- | -- | 0 |")
                continue
            lines.append(
                f"| {mode} | {item['mean_pct']}% | "
                f"[{item['ci95_low_pct']}, {item['ci95_high_pct']}] | "
                f"{item['max_pct']}% | {n} |"
            )
    lines.append("")
    lines.append("**Why this matters:** failures late in the graph bypass more")
    lines.append("upstream tokens than failures at the start. The pitch quotes")
    lines.append("the *max-mode* row (best realistic case in production).")
    lines.append("")

    # ── D3
    d3 = data["d3_max_bypass"]
    lines.append("## D3 -- MAX Token Bypass Observed")
    lines.append("")
    if d3["n_branches"] == 0:
        lines.append("_(no branches)_")
    else:
        lines.append(
            f"**Best single replay: {d3['max_pct']}% of LLM tokens bypassed** "
            f"(across n={d3['n_branches']} replays)"
        )
    lines.append("")

    # ── D4
    d4 = data["d4_replay_success"]
    lines.append("## D4 -- Replay Success Rate")
    lines.append("")
    if d4["n_total_branches"] == 0:
        lines.append("_(no replays attempted)_")
    else:
        ci = (
            f" — 95% CI [{d4['ci95_low_pct']}%, {d4['ci95_high_pct']}%] (Wilson)"
            if "ci95_low_pct" in d4 else ""
        )
        lines.append(
            f"**{d4['success_pct']}% replays produced a valid child run** "
            f"({d4['n_with_child']}/{d4['n_total_branches']}){ci}"
        )
    lines.append("")
    lines.append("This is the empirical reliability of the modify-and-replay flow.")
    lines.append("")

    # ── D5
    d5 = data["d5_mean_bypass"]
    lines.append("## D5 -- Mean Token Bypass Rate (baseline)")
    lines.append("")
    if d5.get("n_branches", 0) == 0:
        lines.append("_(no branches)_")
    else:
        lines.append(
            f"**{d5['mean_pct']}% mean bypass** "
            f"(95% CI [{d5['ci95_low_pct']}, {d5['ci95_high_pct']}], n={d5['n_branches']})"
        )
        lines.append("")
        lines.append(
            f"Across all branches: **{d5['tokens_bypassed_total']:,} tokens** "
            f"bypassed out of **{d5['tokens_parent_total']:,}** that would have "
            f"been re-paid in a full re-run."
        )
    lines.append("")

    # ── Supporting footnote
    lines.append("---")
    lines.append("")
    lines.append("## Supporting (infrastructure footnote, not hero)")
    lines.append("")
    overhead = data["supporting_ingest_overhead_ms"]
    if overhead.get("n", 0) > 0:
        lines.append(
            f"- **Husk ingest overhead**: mean {overhead['mean_ms']} ms "
            f"(95% CI [{overhead['ci95_low_ms']}, {overhead['ci95_high_ms']}], "
            f"p50={overhead['p50_ms']} ms, p95={overhead['p95_ms']} ms). "
            f"Comparable: Langfuse 0.1 ms (batched), LangSmith 132 ms (cloud RTT)."
        )
    storage = data["supporting_storage_efficiency"]
    lines.append(
        f"- **Storage**: {storage['bytes_per_trace']:,} bytes / trace "
        f"({storage['db_size_mb']} MB DB total). "
        f"Comparable: Datadog $0.10/GB; Helicone 1-10 GB tiers."
    )
    lines.append("")
    lines.append("## Reproducibility")
    lines.append("")
    lines.append("```bash")
    lines.append("uv run python benchmark/load_dataset.py --source triviaqa --n 1000")
    lines.append("uv run husk-studio start   # in another terminal")
    lines.append("$env:GROQ_API_KEY = '<your key>'")
    lines.append("BENCH_FAST=1 uv run --group examples python benchmark/run_benchmark.py \\")
    lines.append("    --runs 1000 --topics benchmark/queries_1000.jsonl --concurrency 6")
    lines.append("uv run --group examples python benchmark/real_replays.py --limit 250")
    lines.append("uv run python benchmark/hero_report.py --out benchmark/HERO_METRICS.md")
    lines.append("uv run python benchmark/cost_matrix.py --from-db --out benchmark/COST_MATRIX.md")
    lines.append("```")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Husk hero metrics report (D1-D5 + supporting).")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--json-out", type=Path, default=None)
    args = p.parse_args()

    if not args.db.exists():
        print(f"ERROR: {args.db} not found. Run the benchmark first.", file=sys.stderr)
        return 2

    data = collect(args.db, since=args.since)
    md = render(data, args.db, args.since)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}", file=sys.stderr)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        try:
            print(md)
        except UnicodeEncodeError:
            print(md.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
