// The service's event streams (P2-05 R2) through EventSource: the session cookie goes along
// (same origin), reconnects resume with Last-Event-ID. Frames are named by event type, so each
// type gets a listener.

import type { StoredEvent } from "../api/types";

export const EVENT_TYPES = [
  "build_started",
  "plan_ready",
  "cache_hit",
  "submitted",
  "pending",
  "running",
  "finished",
  "infra_failed",
  "retrying",
  "build_finished",
  "stat_audit_mismatch",
] as const;

/** Open `url`; `onEvent` gets each stored event, `onError` each connection error. Returns a
 * function that closes the stream. */
export function openStream(
  url: string,
  onEvent: (e: StoredEvent) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(url, { withCredentials: true });
  const handle = (m: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(m.data) as StoredEvent);
    } catch {
      /* a frame that is not a stored event: ignored */
    }
  };
  for (const t of EVENT_TYPES) source.addEventListener(t, handle);
  source.onmessage = handle;
  if (onError) source.onerror = onError;
  return () => source.close();
}
