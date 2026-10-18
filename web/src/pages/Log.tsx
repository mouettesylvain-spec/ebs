// R3: an action's log: the stored CAS log, or the live log while it runs (polled every 5 s,
// the runner's flush interval, decision D2). The log is fetched in chunks and drawn
// virtualized (only the rows in view exist in the DOM), with search and jump-to-first-error.

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, fetchLog } from "../api/client";
import { ErrorBox } from "../components/common";
import { LogLines, restarted, visibleRange } from "../lib/logModel";

const ROW = 18; // px, as .log .row in styles.css
const POLL_MS = 5000;
const MAX_BYTES = 256 << 20; // beyond this the viewer stops loading (use `ebs logs`)

type Status = "loading" | "live" | "complete" | "none" | "truncated";

export function LogPage() {
  const { domain = "", build: raw = "", "*": rawAction = "" } = useParams();
  const build = Number(raw);
  const action = decodeURIComponent(rawAction);
  const [model, setModel] = useState(() => new LogLines());
  const [status, setStatus] = useState<Status>("loading");
  const [source, setSource] = useState<"cas" | "live" | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [, redraw] = useState(0);
  const [follow, setFollow] = useState(true);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<number[]>([]);
  const [hit, setHit] = useState(-1);
  const [mark, setMark] = useState<number | null>(null);
  const [scroll, setScroll] = useState({ top: 0, height: 600 });
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let lines = new LogLines();
    let src: "cas" | "live" | null = null;
    setModel(lines);
    setStatus("loading");
    setError(null);
    const step = async () => {
      if (stop) return;
      try {
        const chunk = await fetchLog(build, action, lines.bytes);
        if (stop) return;
        if (chunk === null) {
          setStatus(lines.bytes ? "complete" : "none");
          timer = setTimeout(step, POLL_MS); // not started yet, or between attempts
          return;
        }
        if (src !== null && chunk.source !== src) {
          // The action finished: its stored log replaces the live one (it may differ).
          lines = new LogLines();
          setModel(lines);
          src = chunk.source;
          setSource(src);
          timer = setTimeout(step, 0);
          return;
        }
        if (restarted(lines.bytes, chunk.size)) {
          // A retry restarted the live log: drop the old attempt's lines.
          lines = new LogLines();
          setModel(lines);
          timer = setTimeout(step, 0);
          return;
        }
        src = chunk.source;
        setSource(src);
        if (chunk.data.length) lines.append(chunk.data);
        redraw((n) => n + 1);
        if (lines.bytes >= MAX_BYTES) {
          setStatus("truncated");
        } else if (lines.bytes < chunk.size) {
          timer = setTimeout(step, 0); // more is there already
        } else if (chunk.complete) {
          lines.end();
          setStatus("complete");
        } else {
          setStatus("live");
          timer = setTimeout(step, POLL_MS);
        }
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e : new ApiError(0, String(e)));
      }
    };
    void step();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [build, action]);

  const scrollTo = useCallback((line: number) => {
    const el = box.current;
    if (!el) return;
    el.scrollTop = Math.max(0, line * ROW - el.clientHeight / 3);
    setMark(line);
  }, []);

  // Follow the end of a live log unless the reader scrolled up.
  useEffect(() => {
    const el = box.current;
    if (el && follow && status === "live") el.scrollTop = el.scrollHeight;
  });

  useEffect(() => {
    const found = model.search(query);
    setHits(found);
    setHit(found.length ? 0 : -1);
    if (found.length) scrollTo(found[0]!);
  }, [query, model, model.bytes]);

  const move = (by: number) => {
    if (!hits.length) return;
    const n = (hit + by + hits.length) % hits.length;
    setHit(n);
    scrollTo(hits[n]!);
  };

  const firstError = model.firstError();
  const count = model.count;
  const [first, last] = visibleRange(scroll.top, scroll.height, ROW, count);
  const hitSet = new Set(hits);
  const rows = [];
  for (let i = first; i < last; i++) {
    const cls = ["row", hitSet.has(i) ? "hit" : "", i === mark ? "cur" : "", i === firstError ? "err" : ""].join(" ");
    rows.push(
      <div key={i} className={cls} style={{ top: i * ROW }}>
        <span className="no">{i + 1}</span>
        {model.lines[i]}
      </div>,
    );
  }

  const back = `/d/${encodeURIComponent(domain)}/builds/${build}`;
  return (
    <>
      <h1>
        Log of <span className="mono">{action}</span>
      </h1>
      <p>
        <Link to={back}>Build #{build}</Link> ·{" "}
        <span role="status" aria-live="polite">
          {status === "loading" && "Loading…"}
          {status === "none" && "No log yet: the action has not started, or its log was not kept."}
          {status === "live" && "Live log: refreshed every 5 s."}
          {status === "complete" && `Stored log${source === "cas" ? "" : " (live copy)"}, ${count} lines.`}
          {status === "truncated" && `Showing the first ${MAX_BYTES >> 20} MiB; use \`ebs logs\` for the rest.`}
        </span>
      </p>
      {error && <ErrorBox error={error} />}
      <div className="logbar">
        <label>
          Search
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                move(e.shiftKey ? -1 : 1);
              }
            }}
          />
        </label>
        <button type="button" onClick={() => move(-1)} disabled={!hits.length}>
          ↑ Previous match
        </button>
        <button type="button" onClick={() => move(1)} disabled={!hits.length}>
          ↓ Next match
        </button>
        <span className="muted" aria-live="polite">
          {query ? (hits.length ? `${hit + 1} of ${hits.length}` : "no match") : ""}
        </span>
        <button type="button" onClick={() => scrollTo(firstError)} disabled={firstError < 0}>
          Jump to first error
        </button>
        {status === "live" && (
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
            Follow
          </label>
        )}
      </div>
      <div
        ref={box}
        className="log"
        tabIndex={0}
        role="log"
        aria-label={`Log of ${action}, ${count} lines`}
        data-lines={count}
        onScroll={(e) => {
          const el = e.currentTarget;
          setScroll({ top: el.scrollTop, height: el.clientHeight });
          if (status === "live") setFollow(el.scrollTop + el.clientHeight >= el.scrollHeight - ROW);
        }}
      >
        <div style={{ height: count * ROW, position: "relative" }}>{rows}</div>
      </div>
    </>
  );
}
