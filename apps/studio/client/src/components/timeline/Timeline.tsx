import { useMemo } from "react";

import { ParentSize } from "@visx/responsive";
import { scaleLinear } from "@visx/scale";

import { fmtDuration, fmtTokens, type Span } from "@/lib/api";
import { useTheme } from "@/lib/theme";

interface TimelineProps {
  spans: Span[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

const ROW = 38;
const PADDING_TOP = 28;
const PADDING_BOTTOM = 16;
const LEFT_LABEL_WIDTH = 230;
const BAR_HEIGHT = 14;

const CHROME_LIGHT = { gridline: "#c4c5ca", selectedBar: "#26272c" };
const CHROME_DARK = { gridline: "#3c3d45", selectedBar: "#f2f2f4" };

export function Timeline({ spans, selectedId, onSelect }: TimelineProps) {
  const [theme] = useTheme();
  const CHROME = theme === "dark" ? CHROME_DARK : CHROME_LIGHT;

  if (spans.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-sm text-muted-foreground">
        <div className="text-center">
          <span className="inline-block size-3 animate-pulse rounded-full bg-accent/60 mb-2" />
          <p>Listening for spans…</p>
        </div>
      </div>
    );
  }

  // graph_node spans are per-node wrappers shown in the Graph tab; the timeline
  // shows the actual work (llm / tool / chain), so they'd only be noise here.
  const sorted = useMemo(
    () =>
      spans
        .filter((s) => s.kind !== "graph_node")
        .sort((a, b) => a.started_at - b.started_at),
    [spans],
  );

  const range = useMemo(() => {
    let minT = Infinity;
    let maxT = -Infinity;
    for (const s of sorted) {
      if (s.started_at) minT = Math.min(minT, s.started_at);
      const end = s.finished_at ?? s.started_at + 100;
      if (end) maxT = Math.max(maxT, end);
    }
    if (!isFinite(minT) || !isFinite(maxT) || maxT === minT) {
      return { minT: minT || 0, maxT: (minT || 0) + 1 };
    }
    return { minT, maxT };
  }, [sorted]);

  const totalDuration = range.maxT - range.minT;

  return (
    <div className="h-full overflow-y-auto px-1.5 py-2">
      <ParentSize>
        {({ width }) => {
          const barWidth = Math.max(width - LEFT_LABEL_WIDTH - 30, 80);
          const timeScale = scaleLinear({
            domain: [range.minT, range.maxT],
            range: [0, barWidth],
          });
          const height = PADDING_TOP + sorted.length * ROW + PADDING_BOTTOM;
          return (
            <svg width={width} height={height} className="block">
              {[0, 0.25, 0.5, 0.75, 1].map((p) => {
                const x = LEFT_LABEL_WIDTH + barWidth * p;
                const ms = totalDuration * p;
                return (
                  <g key={p}>
                    <line
                      x1={x}
                      y1={PADDING_TOP - 8}
                      x2={x}
                      y2={height - 6}
                      stroke={CHROME.gridline}
                      strokeWidth={1}
                      strokeDasharray="2 5"
                      opacity={0.6}
                    />
                    <text
                      x={x}
                      y={PADDING_TOP - 14}
                      textAnchor="middle"
                      className="fill-muted-foreground text-[10px]"
                    >
                      {p === 0 ? "0" : fmtDuration(Math.round(ms))}
                    </text>
                  </g>
                );
              })}
              {sorted.map((s, idx) => {
                const y = PADDING_TOP + idx * ROW;
                const x0 = LEFT_LABEL_WIDTH + timeScale(s.started_at);
                const x1 =
                  LEFT_LABEL_WIDTH + timeScale(s.finished_at ?? s.started_at + 50);
                const w = Math.max(x1 - x0, 4);
                const selected = s.id === selectedId;
                const fill = fillForKind(s.kind, s.status, theme);
                return (
                  <g
                    key={s.id}
                    className="cursor-pointer"
                    onClick={() => onSelect(s.id)}
                  >
                    <rect
                      x={4}
                      y={y - 4}
                      width={width - 8}
                      height={ROW - 4}
                      rx={6}
                      ry={6}
                      className={
                        selected
                          ? "fill-secondary/40"
                          : "fill-transparent hover:fill-secondary/30"
                      }
                    />
                    {selected && (
                      <rect
                        x={4}
                        y={y - 4}
                        width={2.5}
                        height={ROW - 4}
                        rx={1.2}
                        ry={1.2}
                        fill={CHROME.selectedBar}
                      />
                    )}
                    <text
                      x={18}
                      y={y + ROW / 2 - 2}
                      className={`fill-foreground text-xs ${selected ? "font-semibold" : ""}`}
                    >
                      <tspan
                        fill={fill.bg}
                        style={{ letterSpacing: 0.5 }}
                      >
                        {kindGlyph(s.kind)}
                      </tspan>{" "}
                      <tspan>{truncate(s.name, 22)}</tspan>
                    </text>
                    <text
                      x={18}
                      y={y + ROW / 2 + 12}
                      className="fill-muted-foreground text-[10px]"
                    >
                      {s.model || s.kind.toUpperCase()}
                      {s.tokens_in || s.tokens_out
                        ? ` · ${fmtTokens(s.tokens_in, s.tokens_out)} tok`
                        : ""}
                    </text>
                    <rect
                      x={x0}
                      y={y + 8}
                      width={w}
                      height={BAR_HEIGHT}
                      rx={3.5}
                      ry={3.5}
                      fill={fill.bg}
                      fillOpacity={selected ? 1 : 0.85}
                      stroke={fill.stroke}
                      strokeWidth={selected ? 1.5 : 0.5}
                    />
                    {w > 50 && s.finished_at && (
                      <text
                        x={x0 + w + 7}
                        y={y + 8 + BAR_HEIGHT - 3}
                        className="fill-muted-foreground text-[10px]"
                      >
                        {fmtDuration(s.finished_at - s.started_at)}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
          );
        }}
      </ParentSize>
    </div>
  );
}

function fillForKind(
  kind: string,
  status: string,
  theme: "light" | "dark",
): { bg: string; stroke: string } {
  if (theme === "dark") {
    if (status === "error") return { bg: "#f2f2f4", stroke: "#f2f2f4" };
    switch (kind) {
      case "llm":
        return { bg: "#9a9ba3", stroke: "#c8c9ce" };
      case "tool":
      case "chain":
        return { bg: "#5a5b63", stroke: "#5a5b63" };
      default:
        return { bg: "#4a4b52", stroke: "#4a4b52" };
    }
  }
  if (status === "error") return { bg: "#26272c", stroke: "#26272c" };
  switch (kind) {
    case "llm":
      return { bg: "#8a8b91", stroke: "#55565c" };
    case "tool":
    case "chain":
      return { bg: "#c4c5ca", stroke: "#c4c5ca" };
    default:
      return { bg: "#bcbdc2", stroke: "#bcbdc2" };
  }
}

function kindGlyph(kind: string): string {
  switch (kind) {
    case "llm":
      return "◆";
    case "tool":
      return "▶";
    case "chain":
      return "●";
    default:
      return "·";
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
