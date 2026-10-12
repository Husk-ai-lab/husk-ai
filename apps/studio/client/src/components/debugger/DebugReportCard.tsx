import { useState } from "react";
import { toast } from "sonner";

import { analyzeRun, type DebugReportRow } from "@/lib/api";
import { Bug, Loader2, Sparkles } from "lucide-react";
import { Link } from "wouter";

interface DebugReportCardProps {
  runId: string;
  report: DebugReportRow | null;
  hasKey: boolean;
  onChanged: () => void;
}

// Plain-language run explainer for a non-engineer PM. It leads with what happened
// and what to change, in prose — not a raw trace or a code diff. (The underlying
// model also proposes a unified diff; that's a code-level affordance, so it isn't
// surfaced here.)
export function DebugReportCard({ runId, report, hasKey, onChanged }: DebugReportCardProps) {
  const [busy, setBusy] = useState(false);

  const analyze = async () => {
    setBusy(true);
    try {
      await analyzeRun(runId);
      toast.success("Husk Studio finished debugging this run");
      onChanged();
    } catch (e) {
      toast.error(`Analysis failed: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  if (!report) {
    return (
      <div className="rounded-lg border border-border/40 bg-secondary/10 p-3">
        <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.16em] text-muted-foreground/80">
          <Bug className="size-3.5 text-accent" />
          Debug this run
        </div>
        {hasKey ? (
          <>
            <p className="mb-2.5 text-[11px] leading-relaxed text-muted-foreground">
              Click once and Husk Studio debugs this run for you — it reads the whole run and
              explains, in plain language, what went wrong and what to change.
            </p>
            <button
              type="button"
              onClick={analyze}
              disabled={busy}
              className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent/90 disabled:opacity-60"
            >
              {busy ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
              Debug this run for me
            </button>
          </>
        ) : (
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            Add an API key in{" "}
            <Link href="/settings" className="text-accent hover:underline">
              Settings
            </Link>{" "}
            to let Husk Studio debug your runs automatically (your key stays on this machine).
          </p>
        )}
      </div>
    );
  }

  const r = report.report;
  return (
    <div className="rounded-lg border border-accent/30 bg-accent/5 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.16em] text-accent">
          <Bug className="size-3.5" />
          Auto-debug · {report.confidence} confidence
        </span>
        <button
          type="button"
          onClick={analyze}
          disabled={busy}
          className="text-[10px] text-muted-foreground hover:text-accent disabled:opacity-60"
        >
          {busy ? "…" : "re-debug"}
        </button>
      </div>

      <Field label="where it went wrong">
        <span className="text-foreground">
          {r.failure_localization.node_id ?? "—"}
        </span>{" "}
        <span className="text-muted-foreground">· {r.failure_class}</span>
      </Field>
      <Field label="what happened">{r.root_cause}</Field>

      {r.evidence.length > 0 && (
        <Field label="why we think so">
          <ul className="list-disc pl-4">
            {r.evidence.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </Field>
      )}

      <Field label="what to change">{r.proposed_fix.summary}</Field>
      <p className="mt-1 text-[10px] italic text-muted-foreground">{r.proposed_fix.rationale}</p>

      {r.missing_information.length > 0 && (
        <Field label="to be sure, check">
          <ul className="list-disc pl-4">
            {r.missing_information.map((m, i) => (
              <li key={i}>{m}</li>
            ))}
          </ul>
        </Field>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mt-2 text-[11px] leading-relaxed">
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground/70">{label}</div>
      <div className="text-foreground/90">{children}</div>
    </div>
  );
}
