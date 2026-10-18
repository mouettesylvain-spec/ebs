import { useRef } from "react";
import { useSearchParams } from "react-router-dom";

/** The page's query parameters and a setter that patches them (an empty value removes one).
 * React Router's setter does not queue updates, so two edits before a render would build on
 * the same old parameters and lose one; the latest are kept here instead. */
export function useQuery(): [URLSearchParams, (patch: Record<string, string>) => void] {
  const [params, setParams] = useSearchParams();
  const latest = useRef(params);
  latest.current = params;
  const set = (patch: Record<string, string>) => {
    const next = new URLSearchParams(latest.current);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    latest.current = next;
    setParams(next);
  };
  return [params, set];
}
