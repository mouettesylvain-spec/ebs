// R5: the releases of a domain by name, and their channels with history. Read-only: releases
// and channels are changed with `ebs release` / `ebs channel`.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { allPages, api, ApiError, must } from "../api/client";
import type { ReleaseView } from "../api/types";
import { Loaded, When, short } from "../components/common";
import { useAsync } from "../lib/useAsync";
import { useQuery } from "../lib/useQuery";

const DEFAULT_CHANNELS = ["stable", "nightly"];

export function ReleasesPage() {
  const { domain = "" } = useParams();
  const [params, patch] = useQuery();
  const name = params.get("name") ?? "";
  const releases = useAsync(
    () =>
      allPages(async (cursor) => {
        const p = await must(
          api.GET("/api/v1/releases", { params: { query: { domain, name: name || undefined, cursor } } }),
        );
        return { items: p.releases, next: p.next };
      }, 5000),
    [domain, name],
  );
  return (
    <>
      <h1>Releases of {domain}</h1>
      <form className="filters" onSubmit={(e) => e.preventDefault()} aria-label="Filter releases">
        <label>
          Name
          <input
            value={name}
            placeholder="team/block"
            onChange={(e) => patch({ name: e.target.value })}
          />
        </label>
      </form>
      <Loaded state={releases}>{(list) => <ReleaseTable domain={domain} releases={list} />}</Loaded>
    </>
  );
}

function ReleaseTable({ domain, releases }: { domain: string; releases: ReleaseView[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const names = [...new Set(releases.map((r) => r.name))].sort();
  if (releases.length === 0) return <p className="muted">No releases.</p>;
  return (
    <>
      {names.map((n) => (
        <section key={n} aria-label={`Release ${n}`}>
          <h2>{n}</h2>
          <Channels domain={domain} name={n} />
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th scope="col">Version</th>
                  <th scope="col">Build</th>
                  <th scope="col">Created</th>
                  <th scope="col">By</th>
                  <th scope="col">Outputs</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {releases
                  .filter((r) => r.name === n)
                  .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
                  .map((r) => (
                    <tr key={r.id}>
                      <td>
                        <button
                          type="button"
                          aria-expanded={open === r.id}
                          onClick={() => setOpen(open === r.id ? null : r.id)}
                        >
                          {r.version}
                        </button>
                        {open === r.id && (
                          <div>
                            {r.notes && <p>{r.notes}</p>}
                            <table aria-label={`Outputs of ${n} ${r.version}`}>
                              <tbody>
                                {Object.entries(r.outputs).map(([k, o]) => (
                                  <tr key={k}>
                                    <td className="mono">{k}</td>
                                    <td>{o.type}, {o.size} bytes</td>
                                    <td>
                                      <Link
                                        className="mono"
                                        to={`/d/${encodeURIComponent(domain)}/provenance?mode=why&id=${encodeURIComponent(o.id)}`}
                                      >
                                        {short(o.id)} (why)
                                      </Link>{" "}
                                      <Link
                                        to={`/d/${encodeURIComponent(domain)}/provenance?mode=used-by&id=${encodeURIComponent(o.id)}`}
                                      >
                                        used by
                                      </Link>{" "}
                                      <Link
                                        to={`/d/${encodeURIComponent(domain)}/provenance?mode=tests&id=${encodeURIComponent(o.id)}`}
                                      >
                                        tests
                                      </Link>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </td>
                      <td>
                        <Link to={`/d/${encodeURIComponent(domain)}/builds/${r.build}`}>#{r.build}</Link>
                      </td>
                      <td><When ts={r.created_at} /></td>
                      <td>{r.created_by}</td>
                      <td>{Object.keys(r.outputs).length}</td>
                      <td>
                        {r.yanked_at ? (
                          <span className="badge st-fail" title={r.yank_reason ?? ""}>
                            <span aria-hidden="true">⊘</span> yanked
                          </span>
                        ) : r.exported_from ? (
                          <span className="badge st-cached">exported from #{r.exported_from}</span>
                        ) : (
                          <span className="badge st-pass">
                            <span aria-hidden="true">✓</span> active
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </>
  );
}

function Channels({ domain, name }: { domain: string; name: string }) {
  const [extra, setExtra] = useState("");
  const [shown, setShown] = useState<string[]>(DEFAULT_CHANNELS);
  const data = useAsync(
    () =>
      Promise.all(
        shown.map(async (channel) => {
          try {
            const view = await must(api.GET("/api/v1/channels", { params: { query: { domain, name, channel } } }));
            const history = await must(
              api.GET("/api/v1/channels/history", { params: { query: { domain, name, channel, limit: 20 } } }),
            );
            return { channel, view: view.channels[0] ?? null, moves: history.moves };
          } catch (e) {
            if (e instanceof ApiError && e.status === 404) return { channel, view: null, moves: [] };
            throw e;
          }
        }),
      ),
    [domain, name, shown.join(",")],
  );
  return (
    <div>
      <Loaded state={data}>
        {(chs) => (
          <ul>
            {chs.map((c) => (
              <li key={c.channel}>
                <strong>{c.channel}</strong>:{" "}
                {c.view ? (
                  <>
                    {c.view.release.version}{" "}
                    <span className="muted">
                      (moved by {c.view.updated_by}, <When ts={c.view.updated_at} />)
                    </span>
                    {c.moves.length > 1 && (
                      <details>
                        <summary>History ({c.moves.length})</summary>
                        <ol>
                          {c.moves.map((m) => (
                            <li key={m.id}>
                              {m.version} by {m.moved_by}, <When ts={m.moved_at} />
                              {m.gate ? ` (gate build #${m.gate.build})` : ""}
                            </li>
                          ))}
                        </ol>
                      </details>
                    )}
                  </>
                ) : (
                  <span className="muted">not set</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Loaded>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          if (extra && !shown.includes(extra)) setShown([...shown, extra]);
          setExtra("");
        }}
      >
        <label>
          Another channel
          <input value={extra} onChange={(e) => setExtra(e.target.value)} size={12} />
        </label>
        <button type="submit">Show</button>
      </form>
    </div>
  );
}
