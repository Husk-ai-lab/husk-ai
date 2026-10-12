import { useEffect, useState } from "react";
import { Link, useLocation, useSearch } from "wouter";

import { FrameworkBadge, FrameworkDot, StatusPill } from "@/components/StatusPill";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fmtAgo, fmtCost, fmtDuration, getRuns, type Run } from "@/lib/api";
import { ArrowLeft, ArrowRight, ListIcon, Search } from "lucide-react";

const STATUS_FILTERS = ["", "success", "error", "running"] as const;

const POLL_MS = 3000;
const SEARCH_DEBOUNCE_MS = 250;

export default function Runs() {
  const [, setLocation] = useLocation();
  const search = useSearch();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>(
    () => new URLSearchParams(search).get("status") ?? "",
  );

  // The query hits a LIKE '%…%' scan server-side, so firing it per keystroke means
  // a full table scan for every character typed. Settle first, then search.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    let alive = true;
    const load = () => {
      getRuns({ q: debouncedQ || undefined, status: statusFilter || undefined })
        .then((r) => alive && setRuns(r))
        .catch((e) => alive && setError(String(e)));
    };
    load(); // always load on mount; only the recurring poll pauses when hidden
    const t = setInterval(() => {
      if (!document.hidden) load();
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [debouncedQ, statusFilter]);

  return (
    <section className="husk-rise px-6 md:px-12 pt-12 md:pt-16 pb-20 max-w-6xl mx-auto">
      <Link
        href="/dashboard"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="size-3.5" />
        Dashboard
      </Link>

      <div className="mt-5 mb-10 flex flex-wrap items-end justify-between gap-3 border-b border-border/30 pb-6">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-accent">
            Recent activity
          </div>
          <h1 className="mt-2 text-5xl md:text-6xl font-normal tracking-[-0.01em]">
            Runs
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Every agent run captured by Husk Studio. Polling every 3s.
          </p>
        </div>
        {runs && (
          <div className="text-xs text-muted-foreground flex items-center gap-2">
            <span className="inline-block size-1.5 rounded-full bg-accent animate-pulse" />
            {runs.length} {runs.length === 1 ? "run" : "runs"}
          </div>
        )}
      </div>

      <div className="mb-5 flex flex-wrap items-center gap-3">
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by script path or run id…"
            className="w-full rounded-md border border-border/40 bg-background/40 py-2 pl-9 pr-3 text-sm focus:border-accent/60 focus:outline-none"
          />
        </div>
        <div className="flex gap-1.5">
          {STATUS_FILTERS.map((s) => (
            <button
              key={s || "all"}
              type="button"
              onClick={() => setStatusFilter(s)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium capitalize transition-colors ${
                statusFilter === s
                  ? "bg-accent text-white"
                  : "bg-secondary/30 text-muted-foreground hover:bg-secondary/50"
              }`}
            >
              {s || "all"}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {runs === null && !error ? (
        <Skeleton />
      ) : runs && runs.length === 0 ? (
        <Empty />
      ) : (
        runs && (
          <div className="overflow-hidden rounded-xl border border-border/30 bg-secondary/10">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent border-border/30">
                  <TableHead className="w-2"></TableHead>
                  <TableHead>Run</TableHead>
                  <TableHead>Agent</TableHead>
                  <TableHead>Models</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Started</TableHead>
                  <TableHead className="text-right">Duration</TableHead>
                  <TableHead className="text-right">Tokens</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((r) => {
                  const tokens =
                    (r.total_tokens_in || 0) + (r.total_tokens_out || 0);
                  const duration = r.finished_at
                    ? r.finished_at - r.started_at
                    : null;
                  return (
                    <TableRow
                      key={r.id}
                      onClick={() => setLocation(`/runs/${r.id}`)}
                      className="cursor-pointer border-border/30 hover:bg-secondary/20 transition-colors"
                    >
                      <TableCell className="pl-4 pr-0">
                        <FrameworkDot framework={r.framework} />
                      </TableCell>
                      <TableCell className="font-medium">
                        {r.script_path || (
                          <span className="font-mono text-xs text-muted-foreground">
                            {r.id.slice(0, 12)}…
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <FrameworkBadge framework={r.framework} />
                      </TableCell>
                      <TableCell>
                        <ModelChips models={r.models} />
                      </TableCell>
                      <TableCell>
                        <StatusPill status={r.status} />
                      </TableCell>
                      <TableCell
                        className="text-muted-foreground"
                        title={new Date(r.started_at).toLocaleString()}
                      >
                        {fmtAgo(r.started_at)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {fmtDuration(duration)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {tokens ? tokens.toLocaleString() : "—"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        <span className={r.total_cost_usd ? "text-foreground" : "text-muted-foreground"}>
                          {fmtCost(r.total_cost_usd)}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )
      )}
    </section>
  );
}

function ModelChips({ models }: { models?: string[] }) {
  if (!models || models.length === 0)
    return <span className="text-xs text-muted-foreground">—</span>;
  const short = (m: string) => m.split("/").pop() ?? m;
  const shown = models.slice(0, 2);
  return (
    <span className="flex flex-wrap items-center gap-1">
      {shown.map((m) => (
        <span
          key={m}
          className="rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-foreground/80"
          title={m}
        >
          {short(m)}
        </span>
      ))}
      {models.length > 2 && (
        <span className="text-[10px] text-muted-foreground">+{models.length - 2}</span>
      )}
    </span>
  );
}

function Skeleton() {
  return (
    <div className="rounded-xl border border-border/30 bg-secondary/10 p-3">
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          className="flex items-center gap-4 border-b border-border/20 px-3 py-4 last:border-0"
        >
          <div className="size-2 rounded-full bg-muted/40" />
          <div className="h-4 w-44 animate-pulse rounded bg-muted/30" />
          <div className="h-4 w-28 animate-pulse rounded bg-muted/30" />
          <div className="h-5 w-16 animate-pulse rounded-full bg-muted/30" />
          <div className="ml-auto h-3.5 w-12 animate-pulse rounded bg-muted/30" />
          <div className="h-3.5 w-12 animate-pulse rounded bg-muted/30" />
        </div>
      ))}
    </div>
  );
}

function Empty() {
  return (
    <div className="rounded-xl border border-dashed border-border/40 bg-secondary/5 px-10 py-16 text-center">
      <div className="mx-auto mb-4 flex size-14 items-center justify-center rounded-xl bg-secondary/40 text-muted-foreground">
        <ListIcon className="size-6" />
      </div>
      <p className="mb-1 text-base font-semibold">No runs yet.</p>
      <p className="mb-6 text-sm text-muted-foreground">
        Connect a framework or seed demo data.
      </p>
      <Link
        href="/onboarding"
        className="inline-flex items-center gap-1.5 rounded-md bg-accent text-white px-4 py-2 text-sm font-semibold hover:bg-accent/90 transition-colors"
      >
        Connect an integration
        <ArrowRight className="size-3.5" />
      </Link>
    </div>
  );
}
