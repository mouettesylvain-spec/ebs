// R1: the builds of a domain, newest first, filtered by user, status, flow (project) and date.
// The filters live in the URL, so a filtered list can be linked.

import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, must } from "../api/client";
import type { BuildStatus, BuildView } from "../api/types";
import { ErrorBox, StateBadge, When, duration } from "../components/common";
import { useQuery } from "../lib/useQuery";

const STATUSES: BuildStatus[] = ["running", "passed", "failed", "infra_failed", "cancelled"];

/** A date input's day as the start of that day in local time, ISO with offset. */
function dayStart(day: string, plusDays = 0): string | undefined {
  if (!day) return undefined;
  const d = new Date(`${day}T00:00:00`);
  d.setDate(d.getDate() + plusDays);
  return d.toISOString();
}

export function BuildsPage() {
  const { domain = "" } = useParams();
  const [params, patch] = useQuery();
  const [builds, setBuilds] = useState<BuildView[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const filters = {
    user: params.get("user") ?? "",
    status: params.get("status") ?? "",
    project: params.get("project") ?? "",
    from: params.get("from") ?? "",
    to: params.get("to") ?? "",
  };
  const key = JSON.stringify([domain, filters]);
  const generation = useRef(0); // each filter change starts one; older answers are dropped

  async function load(cursor?: string) {
    const mine = cursor ? generation.current : ++generation.current;
    const page = await must(
      api.GET("/api/v1/domains/{domain}/builds", {
        params: {
          path: { domain },
          query: {
            user: filters.user || undefined,
            status: (filters.status || undefined) as BuildStatus | undefined,
            project: filters.project || undefined,
            since: dayStart(filters.from),
            until: dayStart(filters.to, 1), // the "to" day is included
            limit: 100,
            cursor,
          },
        },
      }),
    );
    if (mine !== generation.current) return; // a newer filter's answer wins
    setBuilds((old) => [...(cursor && old ? old : []), ...page.builds]);
    setNext(page.next ?? null);
  }

  useEffect(() => {
    setBuilds(null);
    setError(null);
    load().catch(setError);
  }, [key]);

  const set = (name: string, value: string) => patch({ [name]: value });

  return (
    <>
      <h1>Builds of {domain}</h1>
      <form className="filters" aria-label="Filter builds" onSubmit={(e) => e.preventDefault()}>
        <label>
          User
          <input value={filters.user} onChange={(e) => set("user", e.target.value)} />
        </label>
        <label>
          Status
          <select value={filters.status} onChange={(e) => set("status", e.target.value)}>
            <option value="">any</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Flow (project)
          <input value={filters.project} onChange={(e) => set("project", e.target.value)} />
        </label>
        <label>
          From
          <input type="date" value={filters.from} onChange={(e) => set("from", e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={filters.to} onChange={(e) => set("to", e.target.value)} />
        </label>
      </form>
      {error && <ErrorBox error={error} />}
      {!builds && !error && <p role="status">Loading…</p>}
      {builds && (
        <div className="scroll-x">
          <table>
            <caption className="muted" style={{ textAlign: "left" }}>
              {builds.length} build{builds.length === 1 ? "" : "s"}
              {next ? " (more available)" : ""}
            </caption>
            <thead>
              <tr>
                <th scope="col">Build</th>
                <th scope="col">Status</th>
                <th scope="col">Flow</th>
                <th scope="col">User</th>
                <th scope="col">Started</th>
                <th scope="col">Duration</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {builds.map((b) => (
                <tr key={b.id}>
                  <td>
                    <Link to={`/d/${encodeURIComponent(domain)}/builds/${b.id}`}>#{b.id}</Link>
                    {b.ci_job ? <span className="muted"> CI {b.ci_job}</span> : null}
                  </td>
                  <td>
                    <StateBadge state={b.status} />
                  </td>
                  <td>{b.project}</td>
                  <td>{b.user_name}</td>
                  <td>
                    <When ts={b.created_at} />
                  </td>
                  <td>{duration(b.created_at, b.finished_at)}</td>
                  <td>
                    {Object.entries(b.action_counts)
                      .sort()
                      .map(([s, n]) => (
                        <span key={s} style={{ marginRight: 6 }}>
                          <StateBadge state={s} label={`${n} ${s.replace("_", " ")}`} />
                        </span>
                      ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {next && (
        <p>
          <button onClick={() => load(next).catch(setError)}>Load more</button>
        </p>
      )}
    </>
  );
}
