// R4: cache hit rate and saved CPU-hours (from the cached results' recorded resources), and
// license wait time per feature (pending reason durations), over a window of days.

import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, must } from "../api/client";
import { Loaded } from "../components/common";
import { licenseWaits, savings } from "../lib/savings";
import { useAsync } from "../lib/useAsync";

const pct = (r: number | null) => (r === null ? "—" : `${(r * 100).toFixed(1)} %`);
const hours = (h: number) => (h >= 10 ? h.toFixed(0) : h.toFixed(2));

function Bar({ value, max, label }: { value: number; max: number; label: string }) {
  const w = max > 0 ? Math.max(1, Math.round((value / max) * 160)) : 1;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="bar" style={{ width: w }} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

export function MetricsPage() {
  const { domain = "" } = useParams();
  const [days, setDays] = useState(30);
  const stats = useAsync(() => {
    const until = new Date();
    const since = new Date(until.getTime() - days * 86_400_000);
    return must(
      api.GET("/api/v1/domains/{domain}/stats", {
        params: { path: { domain }, query: { since: since.toISOString(), until: until.toISOString() } },
      }),
    );
  }, [domain, days]);

  return (
    <>
      <h1>Metrics of {domain}</h1>
      <div className="filters">
        <label>
          Window
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[1, 7, 30, 90, 365].map((d) => (
              <option key={d} value={d}>
                last {d} day{d === 1 ? "" : "s"}
              </option>
            ))}
          </select>
        </label>
      </div>
      <Loaded state={stats}>
        {(s) => {
          const sv = savings(s.steps);
          const lw = licenseWaits(s.waits);
          const maxSaved = Math.max(0, ...sv.steps.map((x) => x.savedCpuHours));
          const maxWait = Math.max(0, ...lw.features.map((x) => x.hours));
          return (
            <>
              <div className="tiles">
                <div className="tile">
                  <div className="muted">Builds</div>
                  <div className="value">{s.builds}</div>
                </div>
                <div className="tile">
                  <div className="muted">Cache hit rate</div>
                  <div className="value">{pct(sv.hitRate)}</div>
                  <div className="muted">{sv.cached} cached of {sv.cached + sv.ran} actions</div>
                </div>
                <div className="tile">
                  <div className="muted">CPU-hours saved</div>
                  <div className="value">{hours(sv.savedCpuHours)}</div>
                  <div className="muted">{hours(sv.ranCpuHours)} CPU-hours spent</div>
                </div>
                <div className="tile">
                  <div className="muted">License wait</div>
                  <div className="value">{hours(lw.features.reduce((a, f) => a + f.hours, 0))} h</div>
                  <div className="muted">summed over features</div>
                </div>
              </div>
              <h2>Cache by step</h2>
              {sv.steps.length === 0 ? (
                <p className="muted">No actions in this window.</p>
              ) : (
                <div className="scroll-x">
                  <table aria-label="Cache by step">
                    <thead>
                      <tr>
                        <th scope="col">Step</th>
                        <th scope="col">Cached</th>
                        <th scope="col">Ran</th>
                        <th scope="col">Hit rate</th>
                        <th scope="col">CPU-hours saved</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sv.steps.map((x) => (
                        <tr key={x.step}>
                          <td>{x.step}</td>
                          <td>{x.cached}</td>
                          <td>{x.ran}</td>
                          <td>{pct(x.hitRate)}</td>
                          <td>
                            <Bar value={x.savedCpuHours} max={maxSaved} label={hours(x.savedCpuHours)} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <h2>License wait by feature</h2>
              {lw.features.length === 0 ? (
                <p className="muted">No action waited for licenses in this window.</p>
              ) : (
                <div className="scroll-x">
                  <table aria-label="License wait by feature">
                    <thead>
                      <tr>
                        <th scope="col">Feature</th>
                        <th scope="col">Waits</th>
                        <th scope="col">Total (h)</th>
                        <th scope="col">Mean (min)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {lw.features.map((f) => (
                        <tr key={f.feature}>
                          <td className="mono">{f.feature}</td>
                          <td>{f.waits}</td>
                          <td>
                            <Bar value={f.hours} max={maxWait} label={hours(f.hours)} />
                          </td>
                          <td>{f.meanMinutes.toFixed(1)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {lw.otherReasons.length > 0 && (
                <>
                  <h2>Other pending reasons</h2>
                  <table aria-label="Other pending reasons">
                    <thead>
                      <tr>
                        <th scope="col">Reason</th>
                        <th scope="col">Waits</th>
                        <th scope="col">Total (h)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {lw.otherReasons.map((r) => (
                        <tr key={r.reason}>
                          <td>{r.reason}</td>
                          <td>{r.waits}</td>
                          <td>{hours(r.hours)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </>
          );
        }}
      </Loaded>
    </>
  );
}
