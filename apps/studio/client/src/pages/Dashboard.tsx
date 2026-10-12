import { useEffect, useState } from "react";
import { Link } from "wouter";

import { FrameworkDot, StatusPill } from "@/components/StatusPill";
import { StatNumber, Tile } from "@/components/Tile";
import { AlertTriangle, ArrowRight, PlugZap, Sparkles } from "lucide-react";

import {
  fmtAgo,
  fmtCompact,
  fmtCost,
  fmtDuration,
  fmtPct,
  getDashboardSummary,
  getIntegrationsStatus,
  getRuns,
  type AllIntegrationStatus,
  type DashboardSummary,
  type Run,
} from "@/lib/api";

const POLL_MS = 4000;

export default function Dashboard() {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [status, setStatus] = useState<AllIntegrationStatus | null>(null);
  const [failures, setFailures] = useState<Run[]>([]);

  useEffect(() => {
    let alive = true;
    const load = () => {
      getDashboardSummary().then((d) => alive && setData(d)).catch(() => {});
      getIntegrationsStatus().then((s) => alive && setStatus(s)).catch(() => {});
      getRuns({ status: "error", limit: 5 })
        .then((f) => alive && setFailures(f))
        .catch(() => {});
    };
    load(); // always load on mount; only the recurring poll pauses when hidden
    const t = setInterval(() => {
      if (!document.hidden) load();
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const failRate = data && data.totals.runs ? data.totals.errors / data.totals.runs : 0;

  return (
    <section className="husk-rise px-6 md:px-12 pt-12 md:pt-16 pb-20 max-w-6xl mx-auto">
      {/* Hero */}
      <div className="mb-10 md:mb-14">
        <div className="text-xs uppercase tracking-[0.18em] text-accent">
          Debug before you ship · development only
        </div>
        <h1 className="mt-2 text-5xl md:text-7xl font-normal tracking-[-0.01em]">
          What ran, <span className="text-accent italic">and what broke</span>.
        </h1>
        <p className="mt-3 text-sm md:text-base text-muted-foreground max-w-2xl">
          Husk Studio debugs the agent you're building, before it ships. See every run — what it
          did, what it cost, which model handled each step — and when something breaks, one
          click and Husk Studio debugs it for you, in plain language. Local only, never production.
        </p>
      </div>

      {/* Overview header */}
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
          Overview
        </h2>
        <span className="hidden sm:inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="inline-block size-1.5 rounded-full bg-accent animate-pulse" />
          live · on this machine
        </span>
      </div>

      {/* Compact KPI strip — the cost overlook, real data */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-8">
        <Kpi
          label="Cost"
          value={data ? fmtCost(data.totals.cost_usd) : "—"}
          sub={data ? `${fmtCost(data.last_24h.cost_usd)} in 24h` : "loading…"}
          accent
        />
        <Kpi
          label="Tokens"
          value={data ? fmtCompact(data.totals.tokens_in + data.totals.tokens_out) : "—"}
          sub={
            data
              ? `${fmtCompact(data.totals.tokens_in)} in · ${fmtCompact(data.totals.tokens_out)} out`
              : ""
          }
        />
        <Kpi
          label="Avg latency"
          value={data ? fmtDuration(data.totals.avg_latency_ms) : "—"}
          sub="per run"
        />
        <Kpi
          label="Failures"
          value={data ? fmtPct(failRate * 100) : "—"}
          sub={data ? `${data.totals.errors} of ${data.totals.runs} runs` : ""}
        />
      </div>

      {/* Data tiles row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-12">
        <IntegrationTile
          name="Live ingest"
          connected={status?.otel.connected}
          last={status?.otel.last_event_at}
          ever={status?.otel.ever_connected}
        />
        <IntegrationTile
          name="Cursor"
          connected={status?.cursor.connected}
          last={status?.cursor.last_event_at}
          ever={status?.cursor.ever_connected}
        />
        <Tile className="!p-5">
          <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground mb-3">
            Frameworks
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-semibold tabular-nums">
              {status ? status.adapters.length : "—"}
            </span>
            <span className="text-xs text-muted-foreground">
              {status
                ? `${status.adapters.filter((a) => a.connected).length} live`
                : ""}
            </span>
          </div>
        </Tile>
        <Tile
          icon={<AlertTriangle className="size-3.5" />}
          label="Failed runs"
          href="/runs?status=error"
        >
          <StatNumber
            value={data?.totals.errors ?? "—"}
            sub={data?.totals.errors ? "open to debug" : "all clean"}
          />
        </Tile>
      </div>

      {/* Recent activity */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6">
        <div className="md:col-span-2">
          <SectionTitle icon={<Sparkles className="size-3.5" />}>
            Recent runs
          </SectionTitle>
          {data && data.recent_runs.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="overflow-hidden rounded-xl border border-border/30 bg-secondary/10 divide-y divide-border/30">
              {(data?.recent_runs ?? []).map((r) => (
                <Link
                  key={r.id}
                  href={`/runs/${r.id}`}
                  className="flex items-center gap-4 px-5 py-4 hover:bg-secondary/20 transition-colors"
                >
                  <FrameworkDot framework={r.framework} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-foreground">
                      {r.script_path || (
                        <span className="font-mono text-xs text-muted-foreground">
                          {r.id.slice(0, 12)}…
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      {r.framework} · {fmtAgo(r.started_at)}
                    </div>
                  </div>
                  <StatusPill status={r.status} />
                  <div className="hidden sm:block text-right text-xs text-muted-foreground tabular-nums">
                    <div>{fmtDuration(r.duration_ms)}</div>
                    <div className="mt-0.5">
                      {(r.total_tokens_in || 0) + (r.total_tokens_out || 0)} tok
                      {r.total_cost_usd ? ` · ${fmtCost(r.total_cost_usd)}` : ""}
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>

        <div>
          {failures.length > 0 && (
            <div className="mb-6">
              <SectionTitle icon={<AlertTriangle className="size-3.5" />}>
                Needs debugging
              </SectionTitle>
              <div className="overflow-hidden rounded-xl border border-destructive/30 bg-destructive/5 divide-y divide-border/30">
                {failures.map((r) => (
                  <Link
                    key={r.id}
                    href={`/runs/${r.id}`}
                    className="flex items-center gap-3 px-4 py-3 hover:bg-secondary/20 transition-colors"
                  >
                    <FrameworkDot framework={r.framework} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">
                        {r.script_path || `${r.id.slice(0, 12)}…`}
                      </div>
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        {fmtAgo(r.started_at)}
                      </div>
                    </div>
                    <StatusPill status={r.status} />
                  </Link>
                ))}
              </div>
            </div>
          )}
          <SectionTitle icon={<PlugZap className="size-3.5" />}>
            By framework
          </SectionTitle>
          <div className="rounded-xl border border-border/30 bg-secondary/10 p-4">
            {data && data.by_framework.length === 0 ? (
              <p className="text-sm text-muted-foreground py-2">
                Connect a framework to see breakdown.
              </p>
            ) : (
              <ul className="divide-y divide-border/30">
                {(data?.by_framework ?? []).map((f) => (
                  <li
                    key={f.framework}
                    className="flex items-center justify-between py-2.5 text-sm"
                  >
                    <span className="flex items-center gap-2">
                      <FrameworkDot framework={f.framework} />
                      <span className="font-mono text-xs">{f.framework}</span>
                    </span>
                    <span className="text-muted-foreground tabular-nums">
                      {f.count}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="mt-4">
            <Link
              href="/onboarding"
              className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
            >
              Connect your agent
              <ArrowRight className="size-3.5" />
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

function IntegrationTile({
  name,
  connected,
  last,
  ever,
}: {
  name: string;
  connected?: boolean;
  last?: number | null;
  ever?: boolean;
}) {
  const live = !!connected;
  return (
    <Tile className="!p-5">
      <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground mb-3">
        {name}
      </div>
      <div className="flex items-center gap-2">
        <span
          className={`inline-block size-1.5 rounded-full ${
            live ? "bg-accent animate-pulse" : "bg-muted-foreground/40"
          }`}
        />
        <span
          className={`text-sm font-semibold ${live ? "text-foreground" : "text-muted-foreground"}`}
        >
          {live ? "live" : ever ? "idle" : "not connected"}
        </span>
      </div>
      {last && (
        <div className="mt-1 text-[11px] text-muted-foreground">
          {fmtAgo(last)}
        </div>
      )}
    </Tile>
  );
}

function SectionTitle({
  icon,
  children,
}: {
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <h2 className="mb-3 flex items-center gap-2 text-xs uppercase tracking-[0.18em] text-muted-foreground">
      {icon}
      {children}
    </h2>
  );
}

function Kpi({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  accent?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </div>
      <div
        className={`mt-1.5 text-2xl md:text-3xl font-semibold tabular-nums tracking-[-0.02em] ${
          accent ? "text-accent" : "text-foreground"
        }`}
      >
        {value}
      </div>
      {sub && (
        <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{sub}</div>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed border-border/40 bg-secondary/5 p-10 text-center">
      <p className="text-sm text-muted-foreground mb-3">No runs yet.</p>
      <Link
        href="/onboarding"
        className="inline-flex items-center gap-1.5 rounded-md bg-accent text-white px-4 py-2 text-sm font-semibold hover:bg-accent/90 transition-colors"
      >
        Connect your agent
        <ArrowRight className="size-3.5" />
      </Link>
    </div>
  );
}
