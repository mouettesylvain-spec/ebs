// R1: one build: its summary and the live DAG by step. The actions are listed once, then the
// build's event stream (from its first event: plan_ready carries the step graph) keeps them
// current; the stream ends when the build is finished.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { allPages, api, must } from "../api/client";
import type { ActionRow, BuildView } from "../api/types";
import { DagView } from "../components/DagView";
import { ErrorBox, StateBadge, When, duration, short } from "../components/common";
import { applyEvent, groupBySteps, type ActionLite } from "../lib/dagGrouping";
import { regressionSteps } from "../lib/regression";
import { openStream } from "../lib/sse";

const FINAL = new Set(["passed", "failed", "infra_failed", "cancelled"]);

export function BuildPage() {
  const { domain = "", build: raw = "" } = useParams();
  const build = Number(raw);
  const [view, setView] = useState<BuildView | null>(null);
  const [rows, setRows] = useState<ActionRow[] | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [graph, setGraph] = useState<Record<string, string[]>>({});
  const [live, setLive] = useState(false);
  const [tick, setTick] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const actions = useRef(new Map<string, ActionLite>());

  useEffect(() => {
    let closed = false;
    let close = () => {};
    actions.current = new Map();
    setView(null);
    setRows(null);
    setError(null);
    const refresh = () =>
      must(api.GET("/api/v1/builds/{build}", { params: { path: { build } } })).then((v) => {
        if (!closed) setView(v);
        return v;
      });
    (async () => {
      const v = await refresh();
      const list = await allPages(async (cursor) => {
        const page = await must(
          api.GET("/api/v1/builds/{build}/actions", { params: { path: { build }, query: { cursor } } }),
        );
        return { items: page.actions, next: page.next };
      });
      if (closed) return;
      for (const r of list) {
        actions.current.set(r.action_id, {
          action_id: r.action_id,
          step: r.step,
          state: r.state,
          pending_reason: r.pending_reason ?? null,
        });
      }
      setRows(list);
      // Replays from the first event (for plan_ready's step graph), then follows live.
      let pending = false;
      close = openStream(
        `/api/v1/builds/${build}/events`,
        ({ event }) => {
          if (event.type === "plan_ready" && event.data.steps && typeof event.data.steps === "object") {
            setGraph(event.data.steps as Record<string, string[]>);
          }
          if (applyEvent(actions.current, event) && !pending) {
            pending = true;
            setTimeout(() => {
              pending = false;
              setTick((t) => t + 1);
            }, 200); // at most five redraws a second
          }
          if (event.type === "build_finished") {
            close();
            setLive(false);
            refresh().catch(() => {});
          }
        },
        () => {
          // The stream of a finished build ends; EventSource would reconnect forever.
          refresh()
            .then((now) => {
              if (FINAL.has(now.status)) {
                close();
                setLive(false);
              }
            })
            .catch(() => {});
        },
      );
      setLive(!FINAL.has(v.status));
    })().catch((e: unknown) => setError(e instanceof Error ? e : new Error(String(e))));
    return () => {
      closed = true;
      close();
    };
  }, [build]);

  const dag = useMemo(() => groupBySteps(actions.current.values(), graph), [rows, graph, tick]);
  const group = dag.groups.find((g) => g.step === selected);
  const d = `/d/${encodeURIComponent(domain)}/builds/${build}`;
  const hasRegression = rows ? regressionSteps(rows).length > 0 : false;

  if (error) return <ErrorBox error={error} />;
  if (!view) return <p role="status">Loading…</p>;
  return (
    <>
      <h1>
        Build #{view.id} <StateBadge state={view.status} />
      </h1>
      <dl className="tiles">
        <div className="tile">
          <dt className="muted">Flow</dt>
          <dd>{view.project}</dd>
        </div>
        <div className="tile">
          <dt className="muted">User</dt>
          <dd>{view.user_name}{view.ci_job ? ` (CI ${view.ci_job})` : ""}</dd>
        </div>
        <div className="tile">
          <dt className="muted">Started</dt>
          <dd><When ts={view.created_at} /></dd>
        </div>
        <div className="tile">
          <dt className="muted">Duration</dt>
          <dd>{duration(view.created_at, view.finished_at)}</dd>
        </div>
        <div className="tile">
          <dt className="muted">Commit</dt>
          <dd className="mono">{view.flow_commit ? view.flow_commit.slice(0, 12) : "—"}{view.flow_dirty ? " (dirty)" : ""}</dd>
        </div>
        <div className="tile">
          <dt className="muted">Plan</dt>
          <dd className="mono">{short(view.plan_digest)}</dd>
        </div>
      </dl>
      <p>
        <span role="status" aria-live="polite">{live ? "Live: updating as the build runs." : "Finished."}</span>
        {hasRegression && (
          <>
            {" "}
            <Link to={`${d}/regression`}>Regression grid</Link>
          </>
        )}
      </p>
      <h2>Steps</h2>
      {!rows ? (
        <p role="status">Loading actions…</p>
      ) : dag.groups.length === 0 ? (
        <p className="muted">This build has no actions.</p>
      ) : (
        <DagView groups={dag.groups} edges={dag.edges} selected={selected} onSelect={setSelected} />
      )}
      {group && (
        <section aria-label={`Actions of step ${group.step}`}>
          <h2>
            Step {group.step} <span className="muted">({group.total} actions)</span>
          </h2>
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th scope="col">Action</th>
                  <th scope="col">State</th>
                  <th scope="col">Log</th>
                </tr>
              </thead>
              <tbody>
                {group.actions.map((a) => (
                  <tr key={a.action_id}>
                    <td className="mono">{a.action_id}</td>
                    <td>
                      <StateBadge state={a.state} />
                      {a.state === "pending" && a.pending_reason ? (
                        <span className="muted"> (waiting for {a.pending_reason})</span>
                      ) : null}
                    </td>
                    <td>
                      <Link to={`${d}/log/${encodeURIComponent(a.action_id)}`}>log</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
