# Husk benchmark -- hero metrics (OpenRouter Llama + Bootstrap BCa)

- Source: `C:\Users\monti\.husk_engine_real\traces_clean.db` (live SQLite)
- Filter: parent runs with `started_at >= 0`
- CI methodology: BCa Bootstrap, 10,000 resamples (benchmark/bootstrap.py)
- Self-validated against Efron-Tibshirani 1993 GPA example.

## Volume

- Parent runs in this benchmark: **500** (+ 118 replay children = 618 total recorded traces)
  - with real LLM calls: **500**
- Failed parent runs: **118** (23.6%)
- LLM spend (list-price estimate over recorded tokens): **$0.5791**
- Token consumption: 1,482,328 in / 471,885 out

Failure breakdown by injected mode:
  - N4: 38
  - N3: 38
  - N2: 26
  - N1: 9

## D1 -- Replay Wall-Time Speed-up (PRIMARY HERO)

**47.89x faster** -- mean wall-time of a replay vs the parent's full run (95% CI [18.53x, 184.01x], n=118)
- p50: 6.92x   p95: 72.16x   max: 3252.13x

**Why this matters:** every replay short-circuits the upstream nodes.
The wall-clock saving compounds with every iteration in a debug cycle.

## D2 -- Token Bypass Rate per failure mode

| Failure mode | Mean bypass | 95% CI | Max | n |
|---|---:|---:|---:|---:|
| N1 | 0.0% | [0.0, 0.0] | 0.0% | 9 |
| N2 | 0.0% | [0.0, 0.0] | 0.0% | 26 |
| N3 | 41.11% | [36.14, 43.36] | 46.92% | 38 |
| N4 | 88.08% | [87.8, 88.42] | 90.65% | 38 |
| unknown | 7.92% | [3.97, 11.93] | 16.26% | 7 |

**Why this matters:** failures late in the graph bypass more
upstream tokens than failures at the start. The pitch quotes
the *max-mode* row (best realistic case).

## D3 -- MAX Token Bypass Observed

**Best single replay: 90.65% of LLM tokens bypassed** (across n=118 replays)

## D4 -- Replay Success Rate

**100.0% replays produced a valid child run** (118/118) — 95% CI [96.85%, 100.0%] (Wilson)

This is the empirical reliability of the modify-and-replay flow.

## D5 -- Mean Token Bypass Rate (baseline)

**42.07% mean bypass** (95% CI [35.65, 48.81], n=118)

Across all branches: **183,108 tokens** bypassed out of **332,773** that would have been re-paid in a full re-run.

---

## Supporting (infrastructure footnote, not hero)

- **Husk ingest overhead**: mean 3.98 ms (95% CI [3.16, 4.96], p50=0.0 ms, p95=43.0 ms).
- **Storage**: 23,648.1 bytes / trace (13.938 MB DB total), all on the local machine.

## Reproducibility

```bash
uv run python benchmark/load_dataset.py --source triviaqa --n 1000
uv run husk-studio start   # in another terminal
$env:GROQ_API_KEY = '<your key>'
BENCH_FAST=1 uv run --group examples python benchmark/run_benchmark.py \
    --runs 1000 --topics benchmark/queries_1000.jsonl --concurrency 6
uv run --group examples python benchmark/real_replays.py --limit 250
uv run python benchmark/hero_report.py --out benchmark/HERO_METRICS.md
uv run python benchmark/cost_matrix.py --from-db --out benchmark/COST_MATRIX.md
```