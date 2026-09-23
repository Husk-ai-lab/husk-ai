import { useMemo } from "react";

import { fmtCost, fmtDuration, fmtTokens, type GraphNode, type RunGraph } from "@/lib/api";
import { useTheme } from "@/lib/theme";
import { AlertTriangle, Bug } from "lucide-react";

import { layoutGraph, type PositionedEdge } from "./graphLayout";

interface GraphViewProps {
  graph: RunGraph;
  selectedId: string | null;
  onSelect: (nodeId: string) => void;
}

// Status palette, kept in step with the Timeline's vocabulary and the theme
// tokens (a solid ink chip = error, quiet fills = the rest, running is the
// lightest/most active). Each status's `fill` sits close to the page
// background and `name`/`text` close to the foreground, so the pair swaps
// cleanly between light and dark rather than hardcoding one mode.
interface StatusColors {
  fill: string;
  stroke: string;
  dot: string;
  name: string;
  text: string;
}

type StatusKey = "error" | "success" | "skipped" | "running";

const STATUS_LIGHT: Record<StatusKey, StatusColors> = {
  error: { fill: "#26272c", stroke: "#26272c", dot: "#26272c", name: "#f4f4f6", text: "#f4f4f6" },
  success: { fill: "#f3f3f5", stroke: "#c4c5ca", dot: "#8a8b91", name: "#26272c", text: "#26272c" },
  skipped: { fill: "#f8f8f9", stroke: "#e3e3e6", dot: "#bcbdc2", name: "#4a4b51", text: "#8a8b91" },
  running: { fill: "#e9e9ec", stroke: "#8a8b91", dot: "#55565c", name: "#26272c", text: "#26272c" },
};

const STATUS_DARK: Record<StatusKey, StatusColors> = {
  error: { fill: "#f2f2f4", stroke: "#f2f2f4", dot: "#f2f2f4", name: "#1a1b1f", text: "#1a1b1f" },
  success: { fill: "#26272e", stroke: "#3c3d45", dot: "#9a9ba3", name: "#e9e9ec", text: "#c8c9ce" },
  skipped: { fill: "#202127", stroke: "#2c2d34", dot: "#6a6b73", name: "#c8c9ce", text: "#7d7e86" },
  running: { fill: "#2c2d35", stroke: "#9a9ba3", dot: "#d0d1d6", name: "#e9e9ec", text: "#e9e9ec" },
};

const CHROME_LIGHT = { arrow: "#a8a9ae", edge: "#c4c5ca", edgeLabel: "#8a8b91", selectedStroke: "#26272c", flagIcon: "#26272c" };
const CHROME_DARK = { arrow: "#5a5b63", edge: "#3c3d45", edgeLabel: "#9a9ba3", selectedStroke: "#f2f2f4", flagIcon: "#e9e9ec" };

function statusOf(status: Record<StatusKey, StatusColors>, s: string): StatusColors {
  return status[s as StatusKey] ?? status.skipped;
}

export function GraphView({ graph, selectedId, onSelect }: GraphViewProps) {
  const [theme] = useTheme();
  const STATUS = theme === "dark" ? STATUS_DARK : STATUS_LIGHT;
  const CHROME = theme === "dark" ? CHROME_DARK : CHROME_LIGHT;
  const layout = useMemo(() => layoutGraph(graph.nodes, graph.edges), [graph.nodes, graph.edges]);

  const implicated = useMemo(() => {
    const set = new Set<string>();
    const r = graph.debug_report?.report;
    if (r) {
      if (r.failure_localization.node_id) set.add(r.failure_localization.node_id);
      for (const id of r.failure_localization.also_implicated) set.add(id);
    }
    return set;
  }, [graph.debug_report]);

  if (graph.nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted-foreground">
        No graph nodes for this run yet.
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {graph.failure && (
        <div className="flex items-center gap-2 border-b border-destructive/20 bg-destructive/[0.07] px-4 py-2 text-xs">
          <AlertTriangle className="size-3.5 shrink-0 text-destructive" />
          <span className="text-destructive">Failure at</span>
          <span className="font-mono font-semibold text-foreground">{graph.failure.node_id}</span>
          {graph.failure.message && (
            <span className="truncate text-muted-foreground">— {graph.failure.message}</span>
          )}
        </div>
      )}

      <div className="flex items-center gap-4 border-b border-border/30 px-4 py-2 text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        <LegendDot color={STATUS.success.dot} label="success" />
        <LegendDot color={STATUS.error.dot} label="failed" />
        <LegendDot color={STATUS.skipped.dot} label="skipped" dashed />
        <LegendDot color={STATUS.running.dot} label="running" />
      </div>

      <div className="flex-1 overflow-auto p-4">
        <svg width={layout.width} height={layout.height} className="block">
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6.5"
              markerHeight="6.5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill={CHROME.arrow} />
            </marker>
          </defs>

          {layout.edges.map((e, i) => (
            <EdgePath key={`${e.from}-${e.to}-${i}`} e={e} chrome={CHROME} />
          ))}

          {layout.nodes.map(({ node, x, y, w, h }) => {
            const c = statusOf(STATUS, node.status);
            const selected = node.id === selectedId;
            const failure = node.is_failure_point;
            const flagged = implicated.has(node.id);
            return (
              <g
                key={node.id}
                transform={`translate(${x}, ${y})`}
                className="cursor-pointer"
                onClick={() => onSelect(node.id)}
              >
                {failure && (
                  <rect
                    x={-4}
                    y={-4}
                    width={w + 8}
                    height={h + 8}
                    rx={13}
                    fill="none"
                    stroke="#F85149"
                    strokeWidth={1.5}
                    opacity={0.55}
                  />
                )}
                <rect
                  width={w}
                  height={h}
                  rx={10}
                  fill={c.fill}
                  stroke={selected ? CHROME.selectedStroke : c.stroke}
                  strokeWidth={selected ? 2.5 : 1.4}
                  strokeDasharray={node.status === "skipped" ? "5 4" : undefined}
                  opacity={node.status === "skipped" ? 0.85 : 1}
                />
                <circle cx={16} cy={18} r={4} fill={c.dot} />
                <text x={28} y={22} className="text-[13px] font-semibold" fill={c.name}>
                  {truncate(node.name, 20)}
                </text>
                <text x={16} y={42} className="text-[11px]" fill={c.text}>
                  {node.status}
                  {node.tokens_in || node.tokens_out ? ` · ${fmtTokens(node.tokens_in, node.tokens_out)} tok` : ""}
                </text>
                <text x={16} y={58} className="text-[10px]" fill={CHROME.edgeLabel}>
                  {node.duration_ms != null ? fmtDuration(node.duration_ms) : "—"}
                  {node.cost_usd ? ` · ${fmtCost(node.cost_usd)}` : ""}
                </text>
                {failure ? (
                  <AlertTriangle x={w - 26} y={9} width={15} height={15} color="#F85149" />
                ) : flagged ? (
                  <Bug x={w - 26} y={9} width={15} height={15} color={CHROME.flagIcon} />
                ) : null}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}

function EdgePath({ e, chrome }: { e: PositionedEdge; chrome: typeof CHROME_LIGHT }) {
  const midY = (e.y1 + e.y2) / 2;
  const d = `M ${e.x1} ${e.y1} C ${e.x1} ${midY}, ${e.x2} ${midY}, ${e.x2} ${e.y2}`;
  return (
    <g>
      <path
        d={d}
        fill="none"
        stroke={chrome.edge}
        strokeWidth={1.5}
        strokeDasharray={e.conditional ? "5 4" : undefined}
        markerEnd="url(#arrow)"
      />
      {e.conditional && e.label && (
        <text x={(e.x1 + e.x2) / 2 + 6} y={midY - 2} className="text-[10px]" fill={chrome.edgeLabel}>
          {e.label}
        </text>
      )}
    </g>
  );
}

function LegendDot({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block size-2 rounded-full"
        style={dashed ? { border: `1.5px dashed ${color}` } : { background: color }}
      />
      {label}
    </span>
  );
}

export function findNode(graph: RunGraph, id: string | null): GraphNode | null {
  if (!id) return null;
  return graph.nodes.find((n) => n.id === id) ?? null;
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
