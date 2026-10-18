// R2: a regression as a test × seed grid, and its failures clustered by signature. A cell links
// to the action's log; its state is a glyph and text (title and accessible name), not only a
// colour.

import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { allPages, api, must } from "../api/client";
import type { ActionRow, ResultSummary } from "../api/types";
import { Loaded } from "../components/common";
import { buildGrid, cellKey, regressionSteps } from "../lib/regression";
import { clusterFailures } from "../lib/signatureCluster";
import { CELL_STYLES, type CellKind } from "../lib/states";
import { useAsync } from "../lib/useAsync";

const FAILED: CellKind[] = ["fail", "cached-fail"];

export function RegressionPage() {
  const { domain = "", build: raw = "" } = useParams();
  const build = Number(raw);
  const data = useAsync(async () => {
    const rows = await allPages(async (cursor) => {
      const p = await must(api.GET("/api/v1/builds/{build}/actions", { params: { path: { build }, query: { cursor } } }));
      return { items: p.actions, next: p.next };
    });
    const results = await allPages(async (cursor) => {
      const p = await must(api.GET("/api/v1/builds/{build}/results", { params: { path: { build }, query: { cursor } } }));
      return { items: p.results, next: p.next };
    });
    return { rows, results };
  }, [build]);
  return (
    <>
      <h1>
        Regression of <Link to={`/d/${encodeURIComponent(domain)}/builds/${build}`}>build #{build}</Link>
      </h1>
      <Loaded state={data}>{(v) => <Grid domain={domain} build={build} rows={v.rows} results={v.results} />}</Loaded>
    </>
  );
}

function Grid({
  domain,
  build,
  rows,
  results,
}: {
  domain: string;
  build: number;
  rows: ActionRow[];
  results: ResultSummary[];
}) {
  const steps = regressionSteps(rows);
  const [step, setStep] = useState(steps[0] ?? "");
  const [onlyFailed, setOnlyFailed] = useState(false);
  const grid = useMemo(
    () => buildGrid(rows.filter((r) => r.step === step), results),
    [rows, results, step],
  );
  const failures = [...grid.cells.values()].filter((c) => FAILED.includes(c.kind));
  const clusters = clusterFailures(failures.map((c) => ({ action_id: c.action_id, signature: c.signature })));
  const tests = onlyFailed
    ? grid.tests.filter((t) => grid.seeds.some((s) => FAILED.includes(grid.cells.get(cellKey(t, s))?.kind ?? "other")))
    : grid.tests;
  const log = (a: string) => `/d/${encodeURIComponent(domain)}/builds/${build}/log/${encodeURIComponent(a)}`;
  const counts = new Map<CellKind, number>();
  for (const c of grid.cells.values()) counts.set(c.kind, (counts.get(c.kind) ?? 0) + 1);

  if (steps.length === 0) return <p>This build has no simulation step (no action with a test).</p>;
  return (
    <>
      <div className="filters">
        <label>
          Step
          <select value={step} onChange={(e) => setStep(e.target.value)}>
            {steps.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <input type="checkbox" checked={onlyFailed} onChange={(e) => setOnlyFailed(e.target.checked)} />
          Only tests with failures
        </label>
      </div>
      <ul className="legend" aria-label="Legend">
        {(Object.keys(CELL_STYLES) as CellKind[]).map((k) => (
          <li key={k} className={`badge ${CELL_STYLES[k].cls}`}>
            <span aria-hidden="true">{CELL_STYLES[k].glyph}</span> {CELL_STYLES[k].label}: {counts.get(k) ?? 0}
          </li>
        ))}
      </ul>
      <h2>Failure signatures</h2>
      {clusters.length === 0 ? (
        <p className="muted">No failures.</p>
      ) : (
        <table aria-label="Failure signatures">
          <thead>
            <tr>
              <th scope="col">Failures</th>
              <th scope="col">Signature (normalized first error line)</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {clusters.map((c) => (
              <tr key={c.signature}>
                <td>{c.actions.length}</td>
                <td className="mono">{c.signature}</td>
                <td>
                  {c.actions.slice(0, 5).map((a) => (
                    <span key={a} style={{ marginRight: 8 }}>
                      <Link to={log(a)} className="mono">{a}</Link>
                    </span>
                  ))}
                  {c.actions.length > 5 && <span className="muted">and {c.actions.length - 5} more</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2>Tests × seeds</h2>
      <div className="scroll-x">
        <table className="grid" aria-label={`Regression grid of step ${step}`}>
          <thead>
            <tr>
              <th scope="col">Test</th>
              {grid.seeds.map((s) => (
                <th key={s} scope="col">seed {s}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tests.map((t) => (
              <tr key={t}>
                <th scope="row" className="mono">{t}</th>
                {grid.seeds.map((s) => {
                  const c = grid.cells.get(cellKey(t, s));
                  if (!c) return <td key={s} className="cell cell-other"><span aria-label="not run">·</span></td>;
                  const st = CELL_STYLES[c.kind];
                  const name = `${t} seed ${s}: ${st.label}${c.signature ? ` (${c.signature})` : ""}`;
                  return (
                    <td key={s} className={`cell cell-${c.kind}`}>
                      <Link to={log(c.action_id)} className={st.cls} title={name} aria-label={name}>
                        {st.glyph}
                      </Link>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {grid.unplaced.length > 0 && (
        <p className="muted">{grid.unplaced.length} action(s) of this step name no test and are not in the grid.</p>
      )}
    </>
  );
}
