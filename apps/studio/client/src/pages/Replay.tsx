import Editor from "@monaco-editor/react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Link, useLocation, useRoute } from "wouter";

import { Timeline } from "@/components/timeline/Timeline";
import {
  fmtDuration,
  fmtPct,
  getRun,
  getSpans,
  listBranches,
  replayRun,
  shortId,
  type Branch,
  type Run,
  type Span,
} from "@/lib/api";
import {
  ArrowLeft,
  ArrowRight,
  Database,
  ExternalLink,
  PencilLine,
  Play,
  Sparkles,
} from "lucide-react";

const FALLBACK_STATE = '{\n  "topic": "Rome"\n}';

export default function Replay() {
  const [, params] = useRoute("/runs/:id/replay");
  const [, setLocation] = useLocation();
  const runId = params?.id;

  const [run, setRun] = useState<Run | null>(null);
  const [spans, setSpans] = useState<Span[]>([]);
  const [stateJson, setStateJson] = useState(FALLBACK_STATE);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    thread_id?: string;
    child_id?: string;
    state?: unknown;
    note?: string;
  } | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [useCassette, setUseCassette] = useState(false);
  const [linkedChild, setLinkedChild] = useState<Branch | null>(null);
  const [linking, setLinking] = useState(false);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    Promise.all([getRun(runId), getSpans(runId)])
      .then(([r, s]) => {
        if (!alive) return;
        setRun(r);
        setSpans(s);
        if (s.length) setSelectedId(s[0].id);
        const seed = pickInitialState(s);
        if (seed) setStateJson(seed);
      })
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [runId]);

  const runReplay = async () => {
    if (!runId) return;
    setError(null);
    setResult(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(stateJson);
    } catch (e) {
      const msg = `Invalid JSON: ${e}`;
      setError(msg);
      toast.error(msg);
      return;
    }
    setRunning(true);
    setLinkedChild(null);
    const startedAt = Date.now();
    const loadingToast = toast.loading(
      useCassette ? "Replaying from cassette…" : "Replaying graph…",
    );
    try {
      const j = await replayRun({
        run_id: runId,
        span_id: selectedId,
        state_override: parsed,
        use_cassette: useCassette,
      });
      setResult(j);
      toast.success(
        j.thread_id
          ? `New run on thread ${shortId(j.thread_id, 10)}`
          : "Replay queued",
        { id: loadingToast },
      );
      void pollForChild(startedAt);
    } catch (e) {
      const msg = String(e);
      setError(msg);
      toast.error(msg, { id: loadingToast });
    } finally {
      setRunning(false);
    }
  };

  // After a replay the child run lands asynchronously (OTel ingest), then the
  // backend records the parent->child branch. Poll for it so we can show the
  // tokens bypassed and link straight to the new run.
  const pollForChild = async (since: number) => {
    if (!runId) return;
    setLinking(true);
    try {
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 600));
        let branches: Branch[] = [];
        try {
          branches = await listBranches({ parent_run_id: runId });
        } catch {
          continue;
        }
        const fresh = branches
          .filter((b) => b.created_at >= since - 2000)
          .sort((a, b) => b.created_at - a.created_at)[0];
        if (fresh) {
          setLinkedChild(fresh);
          return;
        }
      }
    } finally {
      setLinking(false);
    }
  };

  // Only graph-instrumented runs (those that recorded husk.graph_module) can be
  // re-imported and replayed; plain OTel-observability runs cannot.
  const replayable = spans.some(
    (s) => Boolean((s.attrs as Record<string, unknown> | null)?.["husk.graph_module"]),
  );

  return (
    <section className="px-6 md:px-12 pt-12 pb-16 max-w-6xl mx-auto">
      <Link
        href={runId ? `/runs/${runId}` : "/runs"}
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="size-3.5" />
        Run {runId ? shortId(runId, 16) : ""}
      </Link>

      <div className="mt-5 mb-8">
        <div className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.18em] text-accent">
          <Sparkles className="size-3.5" />
          Time-travel
        </div>
        <h1 className="mt-2 text-5xl md:text-6xl font-normal tracking-[-0.01em] flex items-baseline gap-3">
          <PencilLine className="hidden md:inline-block size-7 text-accent" />
          Modify <span className="text-accent italic">&amp;</span> replay
        </h1>
        {run && (
          <p className="mt-2 text-sm text-muted-foreground">
            Forking {run.framework} from the selected span. The original run is
            preserved.
          </p>
        )}
      </div>

      {run && spans.length > 0 && !replayable && (
        <div className="mb-6 rounded-lg border border-border bg-secondary/50 px-4 py-3 text-sm text-muted-foreground">
          This run is observability-only — no{" "}
          <code className="font-mono text-xs">husk.graph_module</code> was recorded, so it
          can&apos;t be replayed. Replay works on agents running on Husk Studio&apos;s own engine (see{" "}
          <code className="font-mono text-xs">examples/husk_thread.py</code>).
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        <section className="lg:col-span-2 overflow-hidden rounded-xl border border-border/30 bg-secondary/10">
          <div className="border-b border-border/30 bg-secondary/30 px-4 py-2.5 text-[11px] uppercase tracking-[0.16em] text-muted-foreground font-semibold flex items-center">
            <span>Original timeline</span>
            {run?.finished_at && run.started_at && (
              <span className="ml-3 font-mono text-[10px] normal-case tracking-normal">
                {fmtDuration(run.finished_at - run.started_at)}
              </span>
            )}
          </div>
          <div className="h-[55vh] min-h-[400px]">
            <Timeline
              spans={spans}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          </div>
        </section>

        <section className="lg:col-span-3 overflow-hidden rounded-xl border border-border/30 bg-secondary/10">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/30 bg-secondary/30 px-4 py-2.5">
            <span className="flex items-center gap-2 text-[11px] uppercase tracking-[0.16em] text-muted-foreground font-semibold">
              <Sparkles className="size-3.5 text-accent" />
              State override (JSON)
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setUseCassette((v) => !v)}
                title="Replay the LLM calls from the recorded cassette instead of calling the provider — free, deterministic, byte-identical (a changed request still falls through to the real API)."
                className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-medium transition-colors ${
                  useCassette
                    ? "border-foreground/30 bg-secondary/60 text-foreground"
                    : "border-border/50 bg-secondary/20 text-muted-foreground hover:text-foreground"
                }`}
              >
                <Database className="size-3.5" />
                Model-free {useCassette ? "on" : "off"}
              </button>
              <button
                type="button"
                onClick={runReplay}
                disabled={running || !runId || !replayable}
                title={
                  replayable
                    ? undefined
                    : "This run is observability-only — no graph module was recorded, so it can't be replayed."
                }
                className="inline-flex items-center gap-2 rounded-md bg-accent px-4 py-1.5 text-sm font-semibold text-white hover:bg-accent/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Play className="size-3.5" />
                {running ? "Running…" : "Run from here"}
                {!running && <ArrowRight className="size-3.5" />}
              </button>
            </div>
          </div>
          <div className="h-[55vh] min-h-[400px]">
            <Editor
              height="100%"
              defaultLanguage="json"
              value={stateJson}
              onChange={(v) => v != null && setStateJson(v)}
              theme="vs-dark"
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                lineNumbers: "on",
                scrollBeyondLastLine: false,
                tabSize: 2,
                wordWrap: "on",
                padding: { top: 12, bottom: 12 },
              }}
            />
          </div>
        </section>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {result && (
        <div className="mt-6 overflow-hidden rounded-xl border border-foreground/30 bg-secondary/40">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-secondary/60 px-5 py-3">
            <div className="flex items-center gap-3">
              <span className="flex size-8 items-center justify-center rounded-lg bg-secondary text-foreground">
                <Sparkles className="size-4" />
              </span>
              <div>
                <div className="text-sm font-semibold text-foreground">
                  Branch created.
                </div>
                <div className="font-mono text-[11px] text-muted-foreground">
                  {result.thread_id
                    ? `thread ${shortId(result.thread_id, 16)}`
                    : "new thread"}
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={() =>
                setLocation(
                  linkedChild ? `/runs/${linkedChild.child_run_id}` : "/runs",
                )
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent/90"
            >
              {linkedChild ? "Open new run" : "See new run"}
              <ExternalLink className="size-3.5" />
            </button>
          </div>
          {(linking || linkedChild) && (
            <div className="border-b border-border px-5 py-3 text-xs">
              {linkedChild ? (
                <span className="text-muted-foreground">
                  Replay bypassed{" "}
                  <span className="font-semibold text-accent">
                    {fmtPct(linkedChild.token_bypass_pct)}
                  </span>{" "}
                  of the parent&apos;s LLM tokens (
                  {linkedChild.tokens_bypassed.toLocaleString()} of{" "}
                  {linkedChild.parent_llm_tokens.toLocaleString()})
                  {useCassette ? " · served model-free from cassette" : ""}.
                </span>
              ) : (
                <span className="text-muted-foreground">
                  Linking the new run and computing tokens bypassed…
                </span>
              )}
            </div>
          )}
          {result.state ? (
            <pre className="overflow-auto bg-background/40 p-4 font-mono text-xs leading-relaxed">
              {JSON.stringify(result.state, null, 2)}
            </pre>
          ) : (
            <p className="px-5 py-3 text-xs text-muted-foreground">
              {result.note ||
                "The new run will appear in /runs as the OTel exporter flushes."}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function pickInitialState(spans: Span[]): string | null {
  const root = spans.find(
    (s) => s.kind === "chain" && (s.attrs as any)?.["husk.thread_id"],
  );
  if (root) {
    const final = (root.attrs as any)?.["husk.final_state"];
    if (typeof final === "string") {
      try {
        return JSON.stringify(JSON.parse(final.replace(/'/g, '"')), null, 2);
      } catch {
        // fall through
      }
    }
  }
  const candidate = spans.find(
    (s) => s.input_inline && typeof s.input_inline === "object",
  );
  if (candidate?.input_inline && typeof candidate.input_inline === "object") {
    try {
      return JSON.stringify(candidate.input_inline, null, 2);
    } catch {
      return null;
    }
  }
  return null;
}
