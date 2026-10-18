import type { ReactNode } from "react";
import { ApiError, loginUrl } from "../api/client";
import type { Async } from "../lib/useAsync";
import { stateStyle } from "../lib/states";

/** A state as glyph + text in its colour: never colour alone (R6). */
export function StateBadge({ state, label }: { state: string; label?: string }) {
  const s = stateStyle(state);
  return (
    <span className={`badge ${s.cls}`}>
      <span aria-hidden="true">{s.glyph}</span>
      {label ?? s.label}
    </span>
  );
}

export function ErrorBox({ error }: { error: Error }) {
  if (error instanceof ApiError && error.status === 401) {
    return (
      <p className="error" role="alert">
        You are not logged in. <a href={loginUrl()}>Log in</a>
      </p>
    );
  }
  return (
    <p className="error" role="alert">
      {error instanceof ApiError ? `${error.status}: ` : ""}
      {error.message}
    </p>
  );
}

/** Render `children(value)` once `state` is loaded; a status line meanwhile. */
export function Loaded<T>({ state, children }: { state: Async<T>; children: (value: T) => ReactNode }) {
  if (state.status === "loading") return <p role="status">Loading…</p>;
  if (state.status === "error") return <ErrorBox error={state.error} />;
  return <>{children(state.value)}</>;
}

export function When({ ts }: { ts: string | null | undefined }) {
  if (!ts) return <span className="muted">—</span>;
  const d = new Date(ts);
  return <time dateTime={ts}>{d.toLocaleString()}</time>;
}

export function duration(from: string, to: string | null | undefined): string {
  const end = to ? new Date(to).getTime() : Date.now();
  const s = Math.max(0, Math.round((end - new Date(from).getTime()) / 1000));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

export function short(digest: string | null | undefined): string {
  if (!digest) return "—";
  const [, hex] = digest.split(":");
  return hex ? `${hex.slice(0, 12)}…` : digest;
}
