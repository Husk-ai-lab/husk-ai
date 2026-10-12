// Post-auth onboarding: 3 integration cards with copy-paste setup snippets.
// Unlike the pre-auth marketing onboarding, here we already know the user —
// so we focus purely on "wire your framework", not "sign up".

import { useEffect, useState } from "react";
import { Link } from "wouter";

import { Tile } from "@/components/Tile";
import {
  getIntegrationsStatus,
  fmtAgo,
  type AllIntegrationStatus,
  type IntegrationState,
} from "@/lib/api";
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  CircleDashed,
  Copy,
  Network,
  PlugZap,
  Terminal,
} from "lucide-react";
import { toast } from "sonner";

const POLL_MS = 2000;

type Kind = "sdk" | "otel" | "cursor";

const STEPS: Record<
  Kind,
  {
    title: string;
    icon: React.ReactNode;
    blurb: string;
    snippets: { label: string; cmd: string }[];
    statusOf: (s: AllIntegrationStatus) => IntegrationState;
  }
> = {
  sdk: {
    title: "Your agent's SDK",
    icon: <PlugZap className="size-5" />,
    blurb:
      "One line and every OpenAI, Anthropic, or LangGraph call your agent makes streams into Husk Studio — token counts, tool calls, cost, and all.",
    snippets: [
      {
        label: "1. Install for your SDK",
        cmd: "pip install 'husk-studio-shared[openai]'   # or [anthropic] · [langgraph] · [llamaindex]",
      },
      {
        label: "2. Add one line, then run your agent as usual",
        cmd: "from husk_studio_shared import instrument_openai; instrument_openai()",
      },
    ],
    statusOf: (s) => s.otel,
  },
  otel: {
    title: "Any framework (OTel)",
    icon: <Network className="size-5" />,
    blurb:
      "Already emit OpenTelemetry? Point any GenAI emitter (AutoGen, OpenAI Agents SDK, LlamaIndex, Pydantic AI, CrewAI, SmolAgents, Vercel AI SDK) at Husk Studio — no adapter needed.",
    snippets: [
      {
        label: "OTLP endpoint",
        cmd: "http://localhost:7654/v1/traces",
      },
    ],
    statusOf: (s) => s.otel,
  },
  cursor: {
    title: "Cursor",
    icon: <Terminal className="size-5" />,
    blurb:
      "Stream Cursor's file edits and stop signals into Husk Studio so the timeline shows every move your IDE agent makes.",
    snippets: [
      { label: "1. Install the bridge", cmd: "npm install -g husk-cursor-hook" },
      { label: "2. Generate hooks.json in your Cursor project", cmd: "husk-cursor-hook install" },
    ],
    statusOf: (s) => s.cursor,
  },
};

export default function Onboarding() {
  const [open, setOpen] = useState<Kind | null>(null);
  const [status, setStatus] = useState<AllIntegrationStatus | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = () =>
      getIntegrationsStatus().then((s) => alive && setStatus(s)).catch(() => {});
    tick();
    // Pause the recurring poll in a background tab (same rule as Dashboard/Runs).
    const t = setInterval(() => {
      if (!document.hidden) tick();
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  return (
    <section className="px-6 md:px-12 pt-12 pb-20 max-w-6xl mx-auto">
      <Link
        href="/dashboard"
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="size-3.5" />
        Dashboard
      </Link>

      <div className="mt-5 mb-10 border-b border-border/30 pb-6">
        <div className="text-xs uppercase tracking-[0.18em] text-accent">
          Onboarding
        </div>
        <h1 className="mt-2 text-5xl md:text-6xl font-normal tracking-[-0.01em]">
          Wire your stack
        </h1>
        <p className="mt-2 text-sm text-muted-foreground max-w-2xl">
          Connect the agent you're building. Husk Studio observes it on this machine while you
          develop — never in production — and starts receiving events the moment you finish
          a setup.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6">
        {(["sdk", "otel", "cursor"] as Kind[]).map((k) => {
          const s = status ? STEPS[k].statusOf(status) : undefined;
          const live = !!s?.connected;
          const ever = !!s?.ever_connected;
          const step = STEPS[k];
          const active = open === k;
          return (
            <button
              key={k}
              type="button"
              onClick={() => setOpen(active ? null : k)}
              className={`text-left rounded-xl border bg-secondary/10 p-6 transition-all duration-200 ${
                active
                  ? "border-accent/60"
                  : live
                    ? "border-foreground/30"
                    : "border-border/30 hover:border-accent/30"
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="size-10 rounded-lg bg-accent/10 border border-accent/30 grid place-items-center text-accent">
                  {step.icon}
                </div>
                <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider">
                  {live ? (
                    <span className="text-foreground inline-flex items-center gap-1">
                      <CheckCircle2 className="size-3" />
                      live
                    </span>
                  ) : (
                    <span className="text-muted-foreground inline-flex items-center gap-1">
                      <CircleDashed className="size-3" />
                      {ever ? "idle" : "not connected"}
                    </span>
                  )}
                </div>
              </div>
              <div className="mt-4 text-xl font-bold tracking-[-0.01em]">
                {step.title}
              </div>
              <p className="mt-1 text-sm text-muted-foreground leading-relaxed">
                {step.blurb}
              </p>
              <div className="mt-4 text-sm text-accent inline-flex items-center gap-1">
                {active ? "Hide setup" : "Show setup"}
                <ArrowRight className={`size-3.5 transition-transform ${active ? "rotate-90" : ""}`} />
              </div>
            </button>
          );
        })}
      </div>

      {open && (
        <div className="mt-8 rounded-xl border border-accent/30 bg-secondary/10 p-6 md:p-8">
          <div className="text-xs uppercase tracking-[0.18em] text-accent mb-2">
            Setup · {STEPS[open].title}
          </div>
          <h2 className="text-2xl font-bold tracking-[-0.02em] mb-4">
            {STEPS[open].title} in {STEPS[open].snippets.length} step
            {STEPS[open].snippets.length === 1 ? "" : "s"}
          </h2>
          <div className="space-y-4">
            {STEPS[open].snippets.map((s, i) => (
              <Step n={i + 1} label={s.label} key={i}>
                <CodeLine value={s.cmd} />
              </Step>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function Step({
  n,
  label,
  children,
}: {
  n: number;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center gap-3 mb-1.5">
        <span className="flex size-6 items-center justify-center rounded-full bg-accent/15 text-xs font-bold text-accent">
          {n}
        </span>
        <h3 className="text-base font-semibold">{label}</h3>
      </div>
      <div className="ml-9">{children}</div>
    </div>
  );
}

function CodeLine({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 rounded-md border border-border/40 bg-background/40 px-3 py-2.5 font-mono text-sm">
      <span className="select-none text-accent">$</span>
      <span className="flex-1 select-all overflow-x-auto whitespace-nowrap">{value}</span>
      <button
        type="button"
        onClick={() => {
          navigator.clipboard.writeText(value);
          setCopied(true);
          toast.success("Copied");
          setTimeout(() => setCopied(false), 1200);
        }}
        className="inline-flex items-center gap-1 rounded border border-border/50 bg-secondary/30 px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground hover:border-accent/50 hover:text-accent transition-colors"
      >
        <Copy className="size-3" />
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
