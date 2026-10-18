// The build's DAG by step (R1), laid out by ELK (layered, left to right). Each step is a
// focusable button: Tab or the arrow keys move between steps, Enter or Space expands it to its
// actions. A step's state is its glyph and text, not only its colour (R6).

import type { ELK as Elk, ElkNode } from "elkjs/lib/elk-api";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { StepGroup } from "../lib/dagGrouping";
import { stateStyle } from "../lib/states";

const W = 210;
const H = 66;
let elk: Promise<Elk> | null = null;

/** ELK is large (~1.5 MB): loaded on the first layout, in its own chunk. */
function getElk(): Promise<Elk> {
  elk ??= import("elkjs/lib/elk.bundled.js").then((m) => new m.default());
  return elk;
}

interface Placed {
  x: number;
  y: number;
}

interface LaidOut {
  nodes: Map<string, Placed>;
  edges: { id: string; d: string }[];
  width: number;
  height: number;
}

async function layout(steps: string[], edges: [string, string][]): Promise<LaidOut> {
  const input: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.spacing.nodeNode": "24",
      "elk.layered.spacing.nodeNodeBetweenLayers": "48",
    },
    children: steps.map((s) => ({ id: s, width: W, height: H })),
    edges: edges.map(([a, b]) => ({ id: `${a}->${b}`, sources: [a], targets: [b] })),
  };
  const graph = await (await getElk()).layout(input);
  const nodes = new Map<string, Placed>();
  for (const c of graph.children ?? []) nodes.set(c.id, { x: c.x ?? 0, y: c.y ?? 0 });
  const paths = (graph.edges ?? []).map((e) => {
    const sec = e.sections?.[0];
    if (!sec) return { id: e.id, d: "" };
    const pts = [sec.startPoint, ...(sec.bendPoints ?? []), sec.endPoint];
    return { id: e.id, d: pts.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ") };
  });
  return { nodes, edges: paths, width: (graph.width ?? 0) + 8, height: (graph.height ?? 0) + 8 };
}

/** "3 done, 1 failed": the counts of a step in words, for its label. */
export function countsText(g: StepGroup): string {
  return Object.entries(g.counts)
    .sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0))
    .map(([s, n]) => `${n} ${stateStyle(s).label}`)
    .join(", ");
}

export function DagView({
  groups,
  edges,
  selected,
  onSelect,
}: {
  groups: StepGroup[];
  edges: [string, string][];
  selected: string | null;
  onSelect: (step: string | null) => void;
}) {
  const [laid, setLaid] = useState<LaidOut | null>(null);
  const refs = useRef(new Map<string, SVGGElement>());
  const shape = useMemo(() => JSON.stringify([groups.map((g) => g.step), edges]), [groups, edges]);
  useEffect(() => {
    let live = true;
    layout(
      groups.map((g) => g.step),
      edges,
    ).then((l) => live && setLaid(l));
    return () => {
      live = false;
    };
    // Re-layout only when the steps or edges change, not on every state change.
  }, [shape]);

  if (!laid) return <p role="status">Laying out the graph…</p>;

  const order = groups.map((g) => g.step);
  const key = (e: KeyboardEvent, step: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(selected === step ? null : step);
      return;
    }
    const at = order.indexOf(step);
    const to = e.key === "ArrowRight" || e.key === "ArrowDown" ? at + 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? at - 1 : -2;
    if (to >= 0 && to < order.length) {
      e.preventDefault();
      refs.current.get(order[to]!)?.focus();
    }
  };

  return (
    <div className="dag">
      <svg width={laid.width} height={laid.height} role="group" aria-label="Build graph by step">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" fill="currentColor" className="muted" />
          </marker>
        </defs>
        {laid.edges.map((e) => (
          <path key={e.id} className="edge" d={e.d} markerEnd="url(#arrow)" />
        ))}
        {groups.map((g, i) => {
          const p = laid.nodes.get(g.step);
          if (!p) return null;
          const st = stateStyle(g.state);
          const name = g.step.length > 26 ? `${g.step.slice(0, 25)}…` : g.step;
          const counts = countsText(g);
          const line3 = counts.length > 32 ? `${g.total} actions` : counts;
          // The name is the visible text (WCAG 2.5.3); the description has every count.
          const desc = `Step ${g.step}, ${g.total} action${g.total === 1 ? "" : "s"}: ${counts}`;
          return (
            <g
              key={g.step}
              ref={(el) => {
                if (el) refs.current.set(g.step, el);
                else refs.current.delete(g.step);
              }}
              className={`node${selected === g.step ? " sel" : ""}`}
              transform={`translate(${p.x},${p.y})`}
              role="button"
              tabIndex={0}
              aria-pressed={selected === g.step}
              aria-describedby={`dag-desc-${i}`}
              onClick={() => onSelect(selected === g.step ? null : g.step)}
              onKeyDown={(e) => key(e, g.step)}
            >
              <desc id={`dag-desc-${i}`}>{desc}</desc>
              <rect width={W} height={H} rx={6} />
              <text x={10} y={20} fontWeight={700}>
                {name}
              </text>
              <text x={10} y={38} className={`state ${st.cls}`}>
                {st.glyph} {st.label}
              </text>
              <text x={10} y={56} className="muted">
                {line3}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
