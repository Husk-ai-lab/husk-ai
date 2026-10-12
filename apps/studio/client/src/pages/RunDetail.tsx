import { useEffect, useState } from "react";
import { Link, useRoute } from "wouter";

import { DebugReportCard } from "@/components/debugger/DebugReportCard";
import { findNode, GraphView } from "@/components/graph/GraphView";
import { NodeContextPanel } from "@/components/graph/NodeContextPanel";
import { Inspector } from "@/components/inspector/Inspector";
import { FrameworkBadge, StatusPill } from "@/components/StatusPill";
import { Timeline } from "@/components/timeline/Timeline";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import {
  fmtCost,
  fmtDuration,
  fmtPct,
  fmtTokens,
  getDebuggerConfig,
  getDiff,
  getRun,
  getRunBreakdown,
  getRunGraph,
  getSpans,
  listBranches,
  shortId,
  subscribeRun,
  type Branch,
  type DebuggerConfig,
  type Run,
  type RunBreakdown,
  type RunDiff,
  type RunEvent,
  type RunGraph,
  type Span,
} from "@/lib/api";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  GitBranch,
  GitCompare,
  Layers,
  Network,
  PencilLine,
  Rows3,
  Wifi,
  WifiOff,
  X,
  Zap,
} from "lucide-react";

export default function RunDetail() {
  const [, params] = useRoute("/runs/:id");
  const runId = params?.id;

  const [run, setRun] = useState<Run | null>(null);
  const [spans, setSpans] = useState<Span[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [childBranches, setChildBranches] = useState<Branch[]>([]);
  const [parentBranch, setParentBranch] = useState<Branch | null>(null);
  const [compare, setCompare] = useState<{ a: string; b: string } | null>(null);
  const [tab, setTab] = useState<"graph" | "timeline">("graph");
  const [graph, setGraph] = useState<RunGraph | null>(null);
  const [graphLoaded, setGraphLoaded] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [dbgConfig, setDbgConfig] = useState<DebuggerConfig | null>(null);
  const [breakdown, setBreakdown] = useState<RunBreakdown | null>(null);

  const refreshGraph = () => {
    if (!runId) return;
    getRunGraph(runId)
      .then(setGraph)
      .catch(() => {});
  };

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setRun(null);
    setSpans([]);
    setSelectedId(null);
    setError(null);
    setChildBranches([]);
    setParentBranch(null);
    setCompare(null);
    setGraph(null);
    setGraphLoaded(false);
    setSelectedNodeId(null);
    setBreakdown(null);
    setTab("graph");

    Promise.all([getRun(runId), getSpans(runId)])
      .then(([r, s]) => {
        if (!alive) return;
        setRun(r);
        setSpans(s);
        if (s.length) setSelectedId(s[0].id);
      })
      .catch((e) => alive && setError(String(e)));

    getRunGraph(runId)
      .then((g) => {
        if (!alive) return;
        setGraph(g);
        setGraphLoaded(true);
        if (g.nodes.length) {
          setSelectedNodeId(g.failure?.node_id ?? g.nodes[0].id);
        } else {
          // Observability-only run (no agent graph) — the timeline is the useful view.
          setTab("timeline");
        }
      })
      .catch(() => alive && setGraphLoaded(true));
    getRunBreakdown(runId)
      .then((b) => alive && setBreakdown(b))
      .catch(() => {});
    getDebuggerConfig()
      .then((c) => alive && setDbgConfig(c))
      .catch(() => {});

    // Lineage: replays produced from this run (as parent) and, if this run is
    // itself a replay, the branch that produced it (as child).
    listBranches({ parent_run_id: runId })
      .then((b) => alive && setChildBranches(b))
      .catch(() => {});
    listBranches({ child_run_id: runId })
      .then((b) => alive && setParentBranch(b[0] ?? null))
      .catch(() => {});

    return () => {
      alive = false;
    };
  }, [runId]);

  // Live span stream — only for runs still in flight. A finished run gets no new
  // spans, so we skip the socket entirely (less load, and it lets the view settle).
  const runStatus = run?.status;
  useEffect(() => {
    if (!runId || !runStatus) return;
    const finished =
      runStatus === "success" || runStatus === "error" || runStatus === "aborted";
    if (finished) {
      setLive(false);
      return;
    }
    let alive = true;
    const ws = subscribeRun(runId, (ev: RunEvent) => {
      if (!alive) return;
      if (ev.type === "span.replay.batch") {
        if (!ev.spans.length) return;
        setSpans((prev) => mergeSpans(prev, ev.spans));
        setSelectedId((sel) => sel ?? ev.spans[0].id);
      } else if (ev.type === "span.created") {
        setSpans((prev) => mergeSpan(prev, ev.span));
        setSelectedId((sel) => sel ?? ev.span.id);
      }
    });
    ws.onopen = () => alive && setLive(true);
    ws.onclose = () => alive && setLive(false);
    return () => {
      alive = false;
      ws.close();
    };
  }, [runId, runStatus]);

  if (error) {
    return (
      <section className="px-6 md:px-12 py-16 max-w-6xl mx-auto">
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      </section>
    );
  }

  const selected = spans.find((s) => s.id === selectedId) ?? null;
  // A run is replayable only if it carries a graph module to re-import; plain
  // OTel-observability runs (the demo, most traced agents) do not.
  const replayable = spans.some(
    (s) => Boolean((s.attrs as Record<string, unknown> | null)?.["husk.graph_module"]),
  );
  const selectedNode = graph ? findNode(graph, selectedNodeId) : null;
  const duration =
    run?.finished_at && run?.started_at ? run.finished_at - run.started_at : null;
  const totalTokens = (run?.total_tokens_in || 0) + (run?.total_tokens_out || 0);

  return (
    <section className="husk-rise px-6 md:px-12 pt-12 pb-16 max-w-7xl mx-auto">
      <Link
        href="/runs"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="size-3.5" />
        Runs
      </Link>

      <div className="mt-5 mb-6 flex flex-wrap items-end justify-between gap-x-8 gap-y-3 border-b border-border/30 pb-6">
        <div className="min-w-0">
          <div className="text-xs uppercase tracking-[0.18em] text-accent">
            Run detail
          </div>
          <h1 className="mt-2 font-mono text-2xl md:text-3xl font-bold tracking-tight">
            {runId ? shortId(runId, 20) : ""}
          </h1>
          {run && (
            <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-muted-foreground">
              <FrameworkBadge framework={run.framework} />
              <StatusPill status={run.status} />
              {duration && <Stat label="Duration" value={fmtDuration(duration)} />}
              {totalTokens > 0 && (
                <Stat
                  label="Tokens"
                  value={fmtTokens(run.total_tokens_in, run.total_tokens_out)}
                />
              )}
              {run.total_cost_usd ? (
                <Stat label="Cost" value={fmtCost(run.total_cost_usd)} highlight />
              ) : null}
              <Stat label="Spans" value={spans.length.toString()} />
            </div>
          )}
        </div>
        <LiveBadge on={live} />
      </div>

      {run?.status === "error" && run.error_message && (
        <div className="mb-6 rounded-xl border border-foreground/30 bg-secondary/60 p-4">
          <div className="mb-1.5 flex items-center gap-2 text-xs uppercase tracking-[0.16em] text-foreground">
            <AlertTriangle className="size-3.5" /> What failed
          </div>
          <p className="break-words font-mono text-sm text-foreground/90">
            {run.error_message}
          </p>
        </div>
      )}

      <div className="h-[calc(100vh-260px)] min-h-[480px] overflow-hidden rounded-xl border border-border/30 bg-secondary/10">
        <ResizablePanelGroup direction="horizontal">
          <ResizablePanel defaultSize={44} minSize={28}>
            <div className="flex h-full flex-col">
              <div className="flex items-center justify-between border-b border-border/30 bg-secondary/30 px-3 py-2">
                <div className="inline-flex rounded-md border border-border/40 bg-background/40 p-0.5">
                  <TabButton active={tab === "graph"} onClick={() => setTab("graph")}>
                    <Network className="size-3.5" /> Graph
                  </TabButton>
                  <TabButton active={tab === "timeline"} onClick={() => setTab("timeline")}>
                    <Rows3 className="size-3.5" /> Timeline · {spans.length}
                  </TabButton>
                </div>
                {graph?.recursion_limit_hit && (
                  <span className="rounded bg-destructive/15 px-1.5 py-0.5 text-[10px] text-destructive">
                    recursion limit
                  </span>
                )}
              </div>
              <div className="flex-1 overflow-hidden">
                {tab === "graph" ? (
                  graph && graph.nodes.length > 0 ? (
                    <GraphView
                      graph={graph}
                      selectedId={selectedNodeId}
                      onSelect={setSelectedNodeId}
                    />
                  ) : graphLoaded ? (
                    <ObservabilityOnly onTimeline={() => setTab("timeline")} />
                  ) : (
                    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                      Building graph…
                    </div>
                  )
                ) : (
                  <Timeline spans={spans} selectedId={selectedId} onSelect={setSelectedId} />
                )}
              </div>
            </div>
          </ResizablePanel>
          <ResizableHandle withHandle />
          <ResizablePanel defaultSize={36} minSize={26}>
            <Panel title={tab === "graph" ? "Node context" : "Inspector"}>
              {tab === "graph" ? (
                <NodeContextPanel node={selectedNode} />
              ) : (
                <Inspector span={selected} />
              )}
            </Panel>
          </ResizablePanel>
          <ResizableHandle withHandle />
          <ResizablePanel defaultSize={20} minSize={16}>
            <Actions
              span={selected}
              runId={runId ?? ""}
              replayable={replayable}
              childBranches={childBranches}
              parentBranch={parentBranch}
              onCompare={setCompare}
              report={graph?.debug_report ?? null}
              hasKey={!!dbgConfig?.has_key}
              onDebugChanged={refreshGraph}
            />
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>

      {breakdown && breakdown.by_model.length > 0 && (
        <div className="mt-6">
          <ModelBreakdownCard breakdown={breakdown} />
        </div>
      )}

      {compare && (
        <DiffSection a={compare.a} b={compare.b} onClose={() => setCompare(null)} />
      )}
    </section>
  );
}

function ModelBreakdownCard({ breakdown }: { breakdown: RunBreakdown }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-[0.16em] text-muted-foreground">
        <Layers className="size-3.5 text-foreground" />
        Models in this run
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-muted-foreground/70">
            <th className="pb-2 text-left font-medium">Model</th>
            <th className="pb-2 text-right font-medium">Calls</th>
            <th className="pb-2 text-right font-medium">Tokens</th>
            <th className="pb-2 text-right font-medium">Cost</th>
            <th className="pb-2 text-right font-medium">Share</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {breakdown.by_model.map((m) => (
            <tr key={`${m.model}-${m.provider}`}>
              <td className="py-2 pr-2">
                <div className="font-mono text-xs text-foreground" title={m.model}>
                  {(m.model.split("/").pop() ?? m.model).slice(0, 28)}
                </div>
                <div className="text-[10px] text-muted-foreground">
                  {m.provider ?? "—"}
                  {m.errors > 0 && ` · ${m.errors} err`}
                </div>
              </td>
              <td className="py-2 text-right tabular-nums text-muted-foreground">{m.calls}</td>
              <td className="py-2 text-right tabular-nums text-muted-foreground">
                {(m.tokens_in + m.tokens_out).toLocaleString()}
              </td>
              <td className="py-2 text-right tabular-nums text-foreground">{fmtCost(m.cost_usd)}</td>
              <td className="py-2 text-right tabular-nums text-muted-foreground">
                {Math.round(m.cost_share * 100)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ObservabilityOnly({ onTimeline }: { onTimeline: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
      <div className="grid size-11 place-items-center rounded-xl bg-secondary text-muted-foreground">
        <Network className="size-5" />
      </div>
      <p className="text-sm font-semibold text-foreground">Observability-only run</p>
      <p className="max-w-xs text-xs text-muted-foreground">
        No agent graph was recorded for this run, so there's nothing to draw. The
        full step-by-step is in the timeline.
      </p>
      <button
        type="button"
        onClick={onTimeline}
        className="rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground hover:bg-secondary"
      >
        View timeline
      </button>
    </div>
  );
}

function mergeSpan(prev: Span[], incoming: Span): Span[] {
  return mergeSpans(prev, [incoming]);
}

// One pass over the incoming batch instead of a findIndex + sort per span, which
// is O(n²) on the backlog a late-joining client receives for a long run.
function mergeSpans(prev: Span[], incoming: Span[]): Span[] {
  if (!incoming.length) return prev;
  const byId = new Map(prev.map((s) => [s.id, s]));
  let added = false;
  for (const span of incoming) {
    const existing = byId.get(span.id);
    if (existing) byId.set(span.id, { ...existing, ...span });
    else {
      byId.set(span.id, span);
      added = true;
    }
  }
  const next = Array.from(byId.values());
  if (added) next.sort((a, b) => a.started_at - b.started_at);
  return next;
}

function Panel({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border/30 bg-secondary/30 px-4 py-2.5 text-[11px] uppercase tracking-[0.16em] text-muted-foreground font-semibold">
        {title}
      </div>
      <div className="flex-1 overflow-hidden">{children}</div>
    </div>
  );
}

function Stat({
  label,
  value,
  highlight,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground/70">
        {label}
      </span>
      <span
        className={`tabular-nums ${highlight ? "text-accent" : "text-foreground"}`}
      >
        {value}
      </span>
    </span>
  );
}

function LiveBadge({ on }: { on: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs ${
        on
          ? "border-foreground/30 bg-foreground/[0.06] text-foreground"
          : "border-border bg-secondary text-muted-foreground"
      }`}
    >
      {on ? <Wifi className="size-3" /> : <WifiOff className="size-3" />}
      {on ? "live" : "offline"}
    </span>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded px-2.5 py-1 text-[11px] font-semibold transition-colors ${
        active
          ? "bg-accent text-white"
          : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {children}
    </button>
  );
}

function Actions({
  span,
  runId,
  replayable,
  childBranches,
  parentBranch,
  onCompare,
  report,
  hasKey,
  onDebugChanged,
}: {
  span: Span | null;
  runId: string;
  replayable: boolean;
  childBranches: Branch[];
  parentBranch: Branch | null;
  onCompare: (c: { a: string; b: string }) => void;
  report: import("@/lib/api").DebugReportRow | null;
  hasKey: boolean;
  onDebugChanged: () => void;
}) {
  const canReplay = Boolean(span) && replayable;
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border/30 bg-secondary/30 px-4 py-2.5 text-[11px] uppercase tracking-[0.16em] text-muted-foreground font-semibold">
        Actions
      </div>
      <div className="flex flex-1 flex-col gap-2.5 overflow-auto p-4">
        <DebugReportCard
          runId={runId}
          report={report}
          hasKey={hasKey}
          onChanged={onDebugChanged}
        />
        <Link
          href={canReplay ? `/runs/${runId}/replay` : "#"}
          onClick={(e) => {
            if (!canReplay) e.preventDefault();
          }}
          aria-disabled={!canReplay}
          title={
            replayable
              ? undefined
              : "This run is observability-only — no graph module was recorded, so it can't be replayed."
          }
          className={`inline-flex w-full items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-all ${
            canReplay
              ? "bg-accent text-white hover:bg-accent/90"
              : "bg-accent/40 text-white cursor-not-allowed"
          }`}
        >
          <PencilLine className="size-4" />
          Modify and replay
          <ArrowRight className="size-4" />
        </Link>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {replayable
            ? "Fork the thread from the selected span with edited state. The original run is preserved."
            : "Replay needs a graph-instrumented run (it must record husk.graph_module — e.g. an agent on Husk Studio's engine). This run is observability-only."}
        </p>

        {parentBranch && (
          <LineageCard
            title="This run is a replay"
            icon={<GitBranch className="size-3.5 text-foreground" />}
          >
            <BranchRow
              branch={parentBranch}
              otherLabel="parent"
              otherId={parentBranch.parent_run_id}
              onCompare={() =>
                onCompare({ a: parentBranch.parent_run_id, b: parentBranch.child_run_id })
              }
            />
          </LineageCard>
        )}

        {childBranches.length > 0 && (
          <LineageCard
            title={`Replays from this run · ${childBranches.length}`}
            icon={<Zap className="size-3.5 text-accent" />}
          >
            <div className="flex flex-col gap-2">
              {childBranches.map((b) => (
                <BranchRow
                  key={b.id}
                  branch={b}
                  otherLabel="replay"
                  otherId={b.child_run_id}
                  onCompare={() =>
                    onCompare({ a: b.parent_run_id, b: b.child_run_id })
                  }
                />
              ))}
            </div>
          </LineageCard>
        )}

        {!parentBranch && childBranches.length === 0 && (
          <p className="mt-2 rounded-md border border-border/40 bg-secondary/10 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
            No replays yet. Modify &amp; replay forks this run and the new branch
            appears here with the tokens it bypassed.
          </p>
        )}
      </div>
    </div>
  );
}

function LineageCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border/40 bg-secondary/10 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.16em] text-muted-foreground/80">
        {icon}
        {title}
      </div>
      {children}
    </div>
  );
}

function BranchRow({
  branch,
  otherLabel,
  otherId,
  onCompare,
}: {
  branch: Branch;
  otherLabel: string;
  otherId: string;
  onCompare: () => void;
}) {
  return (
    <div className="rounded-md border border-border/30 bg-background/40 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <Link
          href={`/runs/${otherId}`}
          className="font-mono text-[11px] text-foreground hover:text-accent"
        >
          {otherLabel} {shortId(otherId, 8)}
        </Link>
        <button
          type="button"
          onClick={onCompare}
          className="inline-flex items-center gap-1 rounded border border-border/50 px-1.5 py-0.5 text-[10px] text-muted-foreground hover:border-accent/50 hover:text-accent transition-colors"
        >
          <GitCompare className="size-3" />
          diff
        </button>
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-lg font-bold tabular-nums text-accent">
          {fmtPct(branch.token_bypass_pct)}
        </span>
        <span className="text-[10px] text-muted-foreground">
          tokens bypassed
        </span>
      </div>
      <div className="mt-0.5 text-[10px] tabular-nums text-muted-foreground">
        {branch.tokens_bypassed.toLocaleString()} of{" "}
        {branch.parent_llm_tokens.toLocaleString()} saved
        {branch.cost_bypassed_usd > 0 && ` · ${fmtCost(branch.cost_bypassed_usd)}`}
      </div>
    </div>
  );
}

function DiffSection({
  a,
  b,
  onClose,
}: {
  a: string;
  b: string;
  onClose: () => void;
}) {
  const [diff, setDiff] = useState<RunDiff | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDiff(null);
    setErr(null);
    getDiff(a, b)
      .then((d) => alive && setDiff(d))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, [a, b]);

  return (
    <div className="mt-6 overflow-hidden rounded-xl border border-border/30 bg-secondary/10">
      <div className="flex items-center justify-between border-b border-border/30 bg-secondary/30 px-5 py-3">
        <span className="flex items-center gap-2 text-[11px] uppercase tracking-[0.16em] text-muted-foreground font-semibold">
          <GitCompare className="size-3.5 text-accent" />
          Compare · parent vs replay
        </span>
        <button
          type="button"
          onClick={onClose}
          className="text-muted-foreground hover:text-foreground"
          aria-label="Close diff"
        >
          <X className="size-4" />
        </button>
      </div>
      {err && <div className="px-5 py-3 text-sm text-destructive">{err}</div>}
      {diff && (
        <div className="p-5">
          <div className="mb-4 flex flex-wrap items-baseline gap-x-6 gap-y-2">
            <div>
              <span className="text-3xl font-bold tabular-nums text-accent">
                {fmtPct(diff.token_bypass_pct)}
              </span>
              <span className="ml-2 text-xs text-muted-foreground">
                LLM tokens bypassed ({diff.tokens_bypassed.toLocaleString()})
              </span>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <DiffSide title="Parent (full run)" side={diff.a} />
            <DiffSide title="Replay (resumed)" side={diff.b} accent />
          </div>
        </div>
      )}
    </div>
  );
}

function DiffSide({
  title,
  side,
  accent,
}: {
  title: string;
  side: RunDiff["a"];
  accent?: boolean;
}) {
  return (
    <div
      className={`rounded-lg border p-3 ${
        accent ? "border-accent/40 bg-accent/5" : "border-border/40 bg-background/40"
      }`}
    >
      <div className="mb-2 text-[10px] uppercase tracking-[0.16em] text-muted-foreground/80">
        {title}
      </div>
      <div className="font-mono text-[11px] text-muted-foreground">
        {shortId(side.run_id, 12)}
      </div>
      <dl className="mt-2 space-y-1 text-xs">
        <DiffStat k="LLM spans" v={side.llm_spans.toString()} />
        <DiffStat k="LLM tokens" v={side.llm_tokens.toLocaleString()} />
        <DiffStat k="Duration" v={fmtDuration(side.duration_ms)} />
        <DiffStat k="Status" v={side.status} />
      </dl>
    </div>
  );
}

function DiffStat({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted-foreground/70">{k}</dt>
      <dd className="tabular-nums text-foreground">{v}</dd>
    </div>
  );
}
