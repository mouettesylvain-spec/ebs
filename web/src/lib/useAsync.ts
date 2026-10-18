import { useEffect, useState } from "react";

export type Async<T> =
  | { status: "loading" }
  | { status: "error"; error: Error }
  | { status: "ok"; value: T };

/** Run `load` when `deps` change; a stale answer (deps changed meanwhile) is dropped. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]): Async<T> {
  const [state, setState] = useState<Async<T>>({ status: "loading" });
  useEffect(() => {
    let live = true;
    setState({ status: "loading" });
    load().then(
      (value) => live && setState({ status: "ok", value }),
      (error: unknown) =>
        live && setState({ status: "error", error: error instanceof Error ? error : new Error(String(error)) }),
    );
    return () => {
      live = false;
    };
  }, deps);
  return state;
}
