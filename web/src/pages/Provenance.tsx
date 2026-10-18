// R5: the provenance explorer, within the domain: `why` walks up from an artifact id (or an
// action key) to what produced it, `used-by` walks down to what consumed it, and `tests` lists
// the simulation actions downstream with their result. Crossing domains (exports) is the CLI's
// (`ebs why` / `ebs used-by` / `ebs tests`).

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, must } from "../api/client";
import type { ProvenanceAction, ProvenanceGraph } from "../api/types";
import { ErrorBox, StateBadge, When, short } from "../components/common";
import { useQuery } from "../lib/useQuery";

type Mode = "why" | "used-by" | "tests";
const DIGEST = /^(sha256|blake3):[0-9a-f]{64}$/;
const DEPTH = 200; // PROVENANCE_DEPTH_MAX: the whole walk

export function isSim(a: ProvenanceAction): boolean {
  return Boolean(a.rule?.endsWith(".sim"));
}

export function ProvenancePage() {
  const { domain = "" } = useParams();
  const [params, set] = useQuery();
  const mode = (params.get("mode") as Mode | null) ?? "why";
  const id = params.get("id") ?? "";
  const kind = params.get("kind") === "key" ? "key" : "id";
  const [typed, setTyped] = useState(id);
  const [graph, setGraph] = useState<ProvenanceGraph | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => setTyped(id), [id]);
  useEffect(() => {
    setGraph(null);
    setError(null);
    if (!DIGEST.test(id)) return;
    setBusy(true);
    const body =
      kind === "key" ? { keys: [id], ids: [], depth: DEPTH } : { ids: [id], keys: [], depth: DEPTH };
    const call =
      mode === "why"
        ? api.POST("/api/v1/domains/{domain}/provenance/upstream", { params: { path: { domain } }, body })
        : api.POST("/api/v1/domains/{domain}/provenance/downstream", { params: { path: { domain } }, body });
    let live = true; // an answer to an older query is dropped
    must(call)
      .then(
        (g) => live && setGraph(g),
        (e: Error) => live && setError(e),
      )
      .finally(() => live && setBusy(false));
    return () => {
      live = false;
    };
  }, [domain, mode, id, kind]);

  const shown = graph ? (mode === "tests" ? graph.actions.filter(isSim) : graph.actions) : [];
  const d = `/d/${encodeURIComponent(domain)}`;
  return (
    <>
      <h1>Provenance in {domain}</h1>
      <form
        className="filters"
        aria-label="Provenance query"
        onSubmit={(e) => {
          e.preventDefault();
          set({ id: typed.trim() });
        }}
      >
        <fieldset style={{ border: 0, padding: 0, display: "flex", gap: 12 }}>
          <legend className="muted" style={{ fontSize: 12 }}>
            Query
          </legend>
          {(["why", "used-by", "tests"] as Mode[]).map((m) => (
            <label key={m} style={{ flexDirection: "row", alignItems: "center", gap: 4 }}>
              <input type="radio" name="mode" checked={mode === m} onChange={() => set({ mode: m })} />
              {m}
            </label>
          ))}
        </fieldset>
        <label>
          Start from
          <select value={kind} onChange={(e) => set({ kind: e.target.value })}>
            <option value="id">artifact id</option>
            <option value="key">action key</option>
          </select>
        </label>
        <label>
          Digest
          <input value={typed} onChange={(e) => setTyped(e.target.value)} size={50} placeholder="sha256:…" className="mono" />
        </label>
        <button type="submit">Query</button>
      </form>
      {id && !DIGEST.test(id) && <p className="error" role="alert">Not a digest: give sha256:&lt;64 hex digits&gt;.</p>}
      {busy && <p role="status">Walking the provenance graph…</p>}
      {error && <ErrorBox error={error} />}
      {graph && (
        <>
          <p role="status">
            {shown.length} action{shown.length === 1 ? "" : "s"}
            {graph.truncated ? " (the walk stopped at its depth limit)" : ""}.
          </p>
          <div className="scroll-x">
            <table aria-label="Actions">
              <thead>
                <tr>
                  <th scope="col">Build</th>
                  <th scope="col">Action</th>
                  <th scope="col">Rule</th>
                  {mode === "tests" ? (
                    <>
                      <th scope="col">Test</th>
                      <th scope="col">Seed</th>
                    </>
                  ) : (
                    <th scope="col">Params</th>
                  )}
                  <th scope="col">Result</th>
                  <th scope="col">Toolchain</th>
                  <th scope="col">Commit</th>
                  <th scope="col">When</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((a) => (
                  <tr key={`${a.build}/${a.action_id}`}>
                    <td>
                      <Link to={`${d}/builds/${a.build}`}>#{a.build}</Link> <span className="muted">{a.user_name}</span>
                    </td>
                    <td className="mono">
                      <Link to={`${d}/builds/${a.build}/log/${encodeURIComponent(a.action_id)}`}>{a.action_id}</Link>
                    </td>
                    <td>{a.rule ?? "—"}</td>
                    {mode === "tests" ? (
                      <>
                        <td>{String(a.params?.test ?? "—")}</td>
                        <td>{String(a.params?.seed ?? "—")}</td>
                      </>
                    ) : (
                      <td className="mono">
                        {a.params ? Object.entries(a.params).map(([k, v]) => `${k}=${String(v)}`).join(" ") : "—"}
                      </td>
                    )}
                    <td>
                      <StateBadge state={a.status ?? a.state} />
                      {a.cached ? <span className="muted"> cached</span> : null}
                    </td>
                    <td className="mono">{short(a.toolchain)}</td>
                    <td className="mono">
                      {a.flow_commit ? a.flow_commit.slice(0, 12) : "—"}
                      {a.flow_dirty ? " (dirty)" : ""}
                    </td>
                    <td><When ts={a.finished_at ?? a.build_created_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {graph.releases.length > 0 && (
            <>
              <h2>Releases holding these artifacts</h2>
              <ul>
                {graph.releases.map((r) => (
                  <li key={`${r.release}/${r.output}`}>
                    {r.name} {r.version}: <span className="mono">{r.output}</span>
                    {r.yanked ? " (yanked)" : ""}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </>
  );
}
