"""Orchestrator — runs the 10k Research Synthesizer pipeline through Husk.

What it does:
  1. Loads topics from `benchmark/topics.jsonl` (or regenerates them).
  2. Loads the failure schedule (or regenerates it).
  3. Invokes `benchmark.research_agent.graph.invoke` for each (topic, mode).
  4. Each invocation emits OTel spans → Husk backend (port 7654) → SQLite.
  5. Prints progress every N runs.

Flags:
  --runs N              How many to execute (default 10000)
  --start IDX           Skip the first IDX entries (useful for resume)
  --concurrency K       Number of worker threads (default 4)
  --fast                Set BENCH_FAST=1 — collapses node sleeps (recommended)
  --realistic-timing    Override --fast: keep real ~24s/run latencies
  --regenerate          Re-generate topics + failure schedule first

Prereq: Husk backend must be running.  `uv run husk-studio start` in another
terminal. The benchmark itself stays offline (no real LLM, no real HTTP).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent

TOPICS = HERE / "topics.jsonl"
SCHEDULE = HERE / "failure_schedule.jsonl"


def _ensure_inputs(regenerate: bool, count: int, topics_path: Path) -> None:
    """Make sure topics + failure schedule exist; generate on demand."""
    need_topics = regenerate or not topics_path.exists()
    need_sched = regenerate or not SCHEDULE.exists()
    if need_topics and topics_path == TOPICS:
        from benchmark.generate_topics import topic_for

        TOPICS.parent.mkdir(parents=True, exist_ok=True)
        with TOPICS.open("w", encoding="utf-8") as fh:
            for i in range(count):
                fh.write(json.dumps({"index": i, "topic": topic_for(i)}) + "\n")
        print(f"  generated {TOPICS} ({count} topics)")
    elif need_topics:
        raise SystemExit(
            f"topics file {topics_path} not found — generate it first "
            "(e.g. `uv run python benchmark/load_dataset.py --n 500`)"
        )
    if need_sched:
        from benchmark.inject_failures import schedule

        SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
        with SCHEDULE.open("w", encoding="utf-8") as fh:
            for idx, mode in schedule(count):
                fh.write(json.dumps({"index": idx, "failure_mode": mode}) + "\n")
        print(f"  generated {SCHEDULE} ({count} entries)")


def _load_inputs(n: int, start: int, topics_path: Path) -> list[tuple[int, str, str | None]]:
    topics: dict[int, str] = {}
    with topics_path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            topics[row["index"]] = row["topic"]
    sched: dict[int, str | None] = {}
    with SCHEDULE.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            sched[row["index"]] = row["failure_mode"]
    out: list[tuple[int, str, str | None]] = []
    for i in range(start, start + n):
        if i not in topics:
            break
        out.append((i, topics[i], sched.get(i)))
    return out


def _run_one(item: tuple[int, str, str | None]) -> dict:
    idx, topic, fail_mode = item
    # Lazy import inside worker so the OTel tracer can be re-init in workers.
    from benchmark.research_agent.graph import invoke

    result = invoke({"topic": topic, "failure_mode": fail_mode})
    return {
        "index": idx,
        "topic": topic,
        "failure_mode": fail_mode,
        "thread_id": result["thread_id"],
        "status": result["state"].get("status", "ok"),
        "error_node": result["state"].get("error_node"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=10_000)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--fast", action="store_true", default=True)
    p.add_argument(
        "--realistic-timing",
        action="store_true",
        help="Override --fast and keep real per-node latencies (~24s/run).",
    )
    p.add_argument("--regenerate", action="store_true")
    p.add_argument(
        "--topics", type=Path, default=TOPICS,
        help="JSONL file with {index, topic} rows. Default: benchmark/topics.jsonl",
    )
    p.add_argument(
        "--report", type=Path, default=HERE / "results_run.jsonl"
    )
    args = p.parse_args()

    if args.realistic_timing:
        os.environ.pop("BENCH_FAST", None)
    elif args.fast:
        os.environ["BENCH_FAST"] = "1"

    print(f"husk benchmark — {args.runs} runs (start={args.start})")
    print(f"  fast={not args.realistic_timing} concurrency={args.concurrency}")

    _ensure_inputs(args.regenerate, max(args.runs + args.start, 10_000), args.topics)

    items = _load_inputs(args.runs, args.start, args.topics)
    if not items:
        print("nothing to do — check topics/schedule", file=sys.stderr)
        return 2

    args.report.parent.mkdir(parents=True, exist_ok=True)
    fh_report = args.report.open("w", encoding="utf-8")

    t0 = time.time()
    ok = 0
    failed = 0
    try:
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for n, res in enumerate(pool.map(_run_one, items), 1):
                fh_report.write(json.dumps(res) + "\n")
                if res["status"] == "ok":
                    ok += 1
                else:
                    failed += 1
                if n % 100 == 0 or n == len(items):
                    rate = n / max(1e-6, time.time() - t0)
                    print(
                        f"  [{n:>5}/{len(items)}] ok={ok} fail={failed} "
                        f"rate={rate:.1f}/s"
                    )
    finally:
        fh_report.close()

    elapsed = time.time() - t0
    print(
        f"\nDone in {elapsed:.1f}s — {ok} ok, {failed} failed. "
        f"Per-run wall {elapsed / len(items):.2f}s avg."
    )
    print(f"Per-run report: {args.report}")
    print("Run metrics.sql against ~/.husk/traces.db to extract hero numbers.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
