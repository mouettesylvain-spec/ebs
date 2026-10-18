// The metadata service client: typed routes from the OpenAPI document (openapi-fetch), the
// session cookie (same origin: the service serves the dashboard at /ui/) and the CSRF header of
// mutating requests (P2-05). Every answer is what the service authorized (R7): nothing is
// filtered here.

import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

export const CSRF_COOKIE = "ebs_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

export function cookie(name: string): string | null {
  for (const part of document.cookie.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const csrf: Middleware = {
  onRequest({ request }) {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      const token = cookie(CSRF_COOKIE);
      if (token) request.headers.set(CSRF_HEADER, token);
    }
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use(csrf);

/** The data of an openapi-fetch answer, else an ApiError with the service's `detail`. */
export async function must<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (data !== undefined && response.ok) return data;
  const detail =
    error && typeof error === "object" && "detail" in error ? String(error.detail) : response.statusText;
  throw new ApiError(response.status, detail || `HTTP ${response.status}`);
}

/** Every page of a paginated list route (P2-05 R3), following `next`, up to `max` items. */
export async function allPages<T>(
  fetchPage: (cursor: string | undefined) => Promise<{ items: T[]; next?: string | null }>,
  max = 100_000,
): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await fetchPage(cursor);
    out.push(...page.items);
    cursor = page.next ?? undefined;
  } while (cursor && out.length < max);
  return out;
}

export function loginUrl(): string {
  return `/auth/login?next=${encodeURIComponent(location.pathname + location.hash)}`;
}

export interface LogChunk {
  data: Uint8Array;
  source: "cas" | "live";
  size: number;
  complete: boolean;
}

/** Bytes of an action's log from `offset` (P2-06 R3); null while it has none. */
export async function fetchLog(
  build: number,
  actionId: string,
  offset: number,
  limit?: number,
): Promise<LogChunk | null> {
  const q = new URLSearchParams({ offset: String(offset) });
  if (limit) q.set("limit", String(limit));
  const url = `/api/v1/builds/${build}/actions/${encodeURIComponent(actionId)}/log?${q}`;
  const r = await fetch(url, { credentials: "same-origin" });
  if (r.status === 404) return null;
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = ((await r.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, detail);
  }
  return {
    data: new Uint8Array(await r.arrayBuffer()),
    source: r.headers.get("X-Ebs-Log-Source") === "cas" ? "cas" : "live",
    size: Number(r.headers.get("X-Ebs-Log-Size") ?? "0"),
    complete: r.headers.get("X-Ebs-Log-Complete") === "true",
  };
}
