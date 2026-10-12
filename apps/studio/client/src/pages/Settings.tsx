import { useEffect, useState } from "react";
import { Link } from "wouter";
import { toast } from "sonner";

import { Tile } from "@/components/Tile";
import { Switch } from "@/components/ui/switch";
import {
  fmtAgo,
  getDebuggerConfig,
  getDebuggerModels,
  getDebuggerProviders,
  getIntegrationsStatus,
  saveDebuggerConfig,
  type AllIntegrationStatus,
  type DebuggerConfig,
  type DebuggerModel,
} from "@/lib/api";
import {
  ArrowLeft,
  Bug,
  CheckCircle2,
  CircleDashed,
  Database,
  Save,
} from "lucide-react";

const TABLES = [
  {
    name: "runs",
    purpose: "One row per agent invocation.",
    cols: [
      { name: "id", type: "string(26)", note: "ULID or trace_id-derived" },
      { name: "parent_run_id", type: "string?", note: "forked from this run" },
      { name: "framework", type: "string", note: "otel/openai, otel/langgraph…" },
      { name: "status", type: "string", note: "running | success | error" },
      { name: "started_at / finished_at", type: "bigint(ms)" },
      { name: "total_tokens_in / out", type: "int" },
      { name: "total_cost_usd", type: "float" },
    ],
  },
  {
    name: "spans",
    purpose: "Every step inside a run (LLM call, tool, agent decision).",
    cols: [
      { name: "id, run_id, parent_span_id", type: "string" },
      { name: "kind", type: "string", note: "llm | tool | chain" },
      { name: "input_inline / output_inline", type: "json" },
      { name: "tokens_in / out, cost_usd", type: "numeric" },
      { name: "provider, model", type: "string" },
      { name: "attrs", type: "json", note: "raw OTel + husk.graph_module, etc." },
    ],
  },
  {
    name: "snapshots",
    purpose: "Captured state at a checkpoint (used by replay).",
    cols: [
      { name: "id, run_id, span_id", type: "string" },
      { name: "state_ref", type: "string", note: "filesystem ref under ~/.husk/runs/" },
      { name: "rng_state, http_cassette_ref", type: "mixed" },
    ],
  },
  {
    name: "branches",
    purpose: "Parent runs ↔ forks link table.",
    cols: [
      { name: "id, parent_run_id, child_run_id", type: "string" },
      { name: "fork_span_id", type: "string" },
      { name: "override_payload", type: "json", note: "what changed" },
      { name: "label, notes", type: "text?" },
    ],
  },
  {
    name: "debug_reports",
    purpose: "Plain-language explanations of failed runs (no API key stored here).",
    cols: [
      { name: "id, run_id", type: "string" },
      { name: "failure_node, failure_class", type: "string?", note: "where + what" },
      { name: "report_json", type: "json", note: "what happened + what to change" },
      { name: "provider, model, confidence", type: "string" },
      { name: "trigger, applied", type: "mixed", note: "manual|auto · fix applied?" },
    ],
  },
  {
    name: "cursor_events",
    purpose: "IDE observability events from the Cursor / VS Code bridges.",
    cols: [
      { name: "id, hook, project", type: "string" },
      { name: "payload", type: "json", note: "raw IDE hook payload (file edits, terminal commands, stop)" },
      { name: "created_at", type: "bigint(ms)" },
    ],
  },
  {
    name: "http_cassettes",
    purpose: "Recorded HTTP responses for deterministic replay.",
    cols: [
      { name: "id, run_id, span_id", type: "string" },
      { name: "method, url, request_hash", type: "string" },
      { name: "response_status, response_body_ref", type: "mixed" },
      { name: "latency_ms", type: "int" },
    ],
  },
];

export default function Settings() {
  const [status, setStatus] = useState<AllIntegrationStatus | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = () =>
      getIntegrationsStatus()
        .then((s) => alive && setStatus(s))
        .catch(() => {});
    tick();
    // Pause the recurring poll in a background tab (same rule as Dashboard/Runs).
    const t = setInterval(() => {
      if (!document.hidden) tick();
    }, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Roll up the per-framework adapter rows into one "are any adapters live?" state.
  const adapters = status?.adapters ?? [];
  const adaptersState = status
    ? {
        connected: adapters.some((a) => a.connected),
        ever_connected: adapters.some((a) => a.ever_connected),
        last_event_at:
          adapters.reduce((m, a) => Math.max(m, a.last_event_at ?? 0), 0) || null,
      }
    : undefined;

  return (
    <section className="husk-rise px-6 md:px-12 pt-12 pb-20 max-w-6xl mx-auto">
      <Link
        href="/dashboard"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="size-3.5" />
        Overview
      </Link>

      <div className="mt-5 mb-10 border-b border-border/30 pb-6">
        <div className="text-xs uppercase tracking-[0.18em] text-accent">
          Configuration
        </div>
        <h1 className="mt-2 text-5xl md:text-6xl font-normal tracking-[-0.01em]">
          Settings
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Connect the agent you're building, set up the automatic debugger, and see what Husk Studio
          keeps on this machine. Husk Studio is a development tool — it runs on your machine while you
          build, never in production.
        </p>
      </div>

      {/* Integrations */}
      <section className="mb-10">
        <H2>Integrations</H2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 md:gap-4">
          <IntegrationCard
            name="OpenTelemetry GenAI"
            hint="Any framework, passive"
            s={status?.otel}
          />
          <IntegrationCard
            name="Cursor"
            hint="IDE observability — file edits, stop signals"
            s={status?.cursor}
          />
          <IntegrationCard
            name="Framework adapters"
            hint="OpenAI, Anthropic, LangGraph…"
            s={adaptersState}
          />
        </div>
      </section>

      {/* Automatic debugger (BYOK) */}
      <section className="mb-10">
        <H2 icon={<Bug className="size-3.5" />}>Automatic debugger (BYOK)</H2>
        <DebuggerSettings />
      </section>

      {/* Schema */}
      <section className="mb-10">
        <H2 icon={<Database className="size-3.5" />}>Database schema</H2>
        <p className="mb-4 text-sm text-muted-foreground">
          Husk Studio persists everything in a single SQLite file at{" "}
          <code className="rounded bg-secondary/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
            ~/.husk/traces.db
          </code>
          .
        </p>
        <div className="space-y-2.5">
          {TABLES.map((t) => (
            <details
              key={t.name}
              className="group overflow-hidden rounded-xl border border-border/30 bg-secondary/10 open:border-accent/40"
            >
              <summary className="flex cursor-pointer items-center justify-between gap-4 px-5 py-3.5 hover:bg-secondary/20 transition-colors">
                <div className="min-w-0">
                  <div className="font-mono text-sm font-semibold">{t.name}</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {t.purpose}
                  </div>
                </div>
                <span className="rounded-md border border-border/40 bg-secondary/40 px-2 py-0.5 font-mono text-[10px] text-muted-foreground transition-transform group-open:rotate-180">
                  ▼
                </span>
              </summary>
              <div className="border-t border-border/30 bg-background/40 px-5 py-3">
                <ul className="divide-y divide-border/30 text-sm">
                  {t.cols.map((c) => (
                    <li
                      key={c.name}
                      className="flex flex-wrap items-baseline justify-between gap-2 py-1.5"
                    >
                      <span className="font-mono text-xs">{c.name}</span>
                      <span className="flex items-baseline gap-2">
                        <span className="rounded bg-secondary/30 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                          {c.type}
                        </span>
                        {c.note && (
                          <span className="text-xs text-muted-foreground">
                            {c.note}
                          </span>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </details>
          ))}
        </div>
      </section>
    </section>
  );
}

// Fallback if the providers endpoint can't be reached; Regolo is the default.
const FALLBACK_PROVIDERS = ["regolo", "anthropic", "openai", "openrouter"];

function DebuggerSettings() {
  const [cfg, setCfg] = useState<DebuggerConfig | null>(null);
  const [providers, setProviders] = useState<string[]>(FALLBACK_PROVIDERS);
  const [models, setModels] = useState<DebuggerModel[]>([]);
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getDebuggerProviders()
      .then((p) => p.providers.length && setProviders(p.providers))
      .catch(() => {});
    getDebuggerConfig()
      .then((c) => {
        setCfg(c);
        getDebuggerModels(c.provider)
          .then((m) => setModels(m.models))
          .catch(() => {});
      })
      .catch(() => {});
  }, []);

  const patch = (p: Partial<DebuggerConfig>) =>
    setCfg((c) => (c ? { ...c, ...p } : c));

  const onProviderChange = (provider: string) => {
    patch({ provider });
    getDebuggerModels(provider)
      .then((m) => {
        setModels(m.models);
        if (m.models.length) patch({ model: m.models[0].id });
      })
      .catch(() => {});
  };

  const save = async () => {
    if (!cfg) return;
    setSaving(true);
    try {
      const next = await saveDebuggerConfig({
        provider: cfg.provider,
        model: cfg.model,
        auto_analyze: cfg.auto_analyze,
        ...(apiKey ? { api_key: apiKey } : {}),
      });
      setCfg(next);
      setApiKey("");
      toast.success("Saved (key stays on this machine)");
    } catch (e) {
      toast.error(`Save failed: ${String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  if (!cfg) {
    return (
      <div className="rounded-xl border border-border/30 bg-secondary/10 p-5 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border/30 bg-secondary/10 p-5 md:p-6">
      <p className="mb-4 text-xs text-muted-foreground">
        When a run fails, Husk Studio debugs it for you: one click (or automatically) and it
        explains, in plain language, what went wrong and what to change. It uses a
        model you bring a key for. The key is stored locally in{" "}
        <code className="rounded bg-secondary/40 px-1.5 py-0.5 font-mono text-[11px]">
          ~/.husk/secrets.json
        </code>{" "}
        and sent only from this machine straight to the provider: never to a Husk Studio
        server, never written into traces or exports.
      </p>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Labeled label="Provider">
          <select
            value={cfg.provider}
            onChange={(e) => onProviderChange(e.target.value)}
            className="w-full rounded-md border border-border/40 bg-background/40 px-3 py-2 text-sm focus:border-accent/60 focus:outline-none"
          >
            {providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </Labeled>

        <Labeled label="Model">
          <select
            value={cfg.model}
            onChange={(e) => patch({ model: e.target.value })}
            className="w-full rounded-md border border-border/40 bg-background/40 px-3 py-2 font-mono text-sm focus:border-accent/60 focus:outline-none"
          >
            {models.length === 0 && <option value={cfg.model}>{cfg.model}</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id} ({Math.round(m.context_window / 1000)}K ctx)
              </option>
            ))}
          </select>
        </Labeled>
      </div>

      <div className="mt-4">
        <Labeled label={`API key${cfg.has_key ? " (a key is set)" : ""}`}>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={cfg.has_key ? "•••••••••• (leave blank to keep)" : "Paste your API key"}
            autoComplete="off"
            className="w-full rounded-md border border-border/40 bg-background/40 px-3 py-2 font-mono text-sm focus:border-accent/60 focus:outline-none"
          />
        </Labeled>
      </div>

      <div className="mt-5 space-y-3">
        <ToggleRow
          label="Debug failed runs automatically"
          hint="Husk Studio debugs a run the moment it finishes in error — no click needed."
          checked={cfg.auto_analyze}
          onChange={(v) => patch({ auto_analyze: v })}
        />
      </div>

      <button
        type="button"
        onClick={save}
        disabled={saving}
        className="mt-5 inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent/90 disabled:opacity-60"
      >
        <Save className="size-3.5" />
        {saving ? "Saving…" : "Save"}
      </button>
    </div>
  );
}

function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
    </label>
  );
}

function ToggleRow({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <div className="text-sm font-medium">{label}</div>
        <div className="text-[11px] text-muted-foreground">{hint}</div>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function H2({
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

function IntegrationCard({
  name,
  hint,
  s,
}: {
  name: string;
  hint: string;
  s?: { connected: boolean; ever_connected: boolean; last_event_at: number | null };
}) {
  const live = !!s?.connected;
  const ever = !!s?.ever_connected;
  return (
    <div
      className={`rounded-xl border bg-secondary/10 p-4 transition-colors ${
        live ? "border-foreground/30" : "border-border/30"
      }`}
    >
      <div className="flex items-center gap-3">
        <span
          className={`flex size-9 items-center justify-center rounded-lg ${
            live
              ? "bg-secondary text-foreground"
              : "bg-secondary/40 text-muted-foreground"
          }`}
        >
          {live ? (
            <CheckCircle2 className="size-5" />
          ) : (
            <CircleDashed className="size-5" />
          )}
        </span>
        <div className="min-w-0">
          <div className="text-sm font-semibold">{name}</div>
          <div className="text-[11px] text-muted-foreground">{hint}</div>
        </div>
      </div>
      <div className="mt-3 flex items-center gap-1.5 text-xs">
        <span
          className={`inline-block size-1.5 rounded-full ${
            live ? "bg-accent animate-pulse" : "bg-muted-foreground/40"
          }`}
        />
        <span className={live ? "text-foreground" : "text-muted-foreground"}>
          {live
            ? `live · ${fmtAgo(s?.last_event_at)}`
            : ever
              ? `idle · ${fmtAgo(s?.last_event_at)}`
              : "not connected yet"}
        </span>
      </div>
    </div>
  );
}
