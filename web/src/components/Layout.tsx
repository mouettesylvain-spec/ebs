import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, must } from "../api/client";
import type { Whoami } from "../api/types";
import { useAsync } from "../lib/useAsync";
import { ErrorBox } from "./common";

export const Me = createContext<Whoami | null>(null);
export const useMe = () => useContext(Me);

/** The domain of the current page (`/d/:domain/...`); the layout is above the routes that name
 * it, so it reads the location rather than its own (empty) params. */
export function useDomain(): string | undefined {
  const m = /^\/d\/([^/]+)/.exec(useLocation().pathname);
  return m ? decodeURIComponent(m[1]!) : undefined;
}

type Theme = "system" | "light" | "dark";

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("ebs-theme") as Theme | null) ?? "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("ebs-theme", theme);
    } catch {
      /* private mode */
    }
  }, [theme]);
  return [theme, setTheme];
}

function DomainPicker({ me }: { me: Whoami }) {
  const domain = useDomain();
  const navigate = useNavigate();
  const [typed, setTyped] = useState(domain ?? "");
  if (me.domains) {
    return (
      <label>
        Domain
        <select
          value={domain ?? ""}
          onChange={(e) => navigate(`/d/${encodeURIComponent(e.target.value)}/builds`)}
        >
          {!domain && <option value="">choose…</option>}
          {me.domains.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </label>
    );
  }
  // An unscoped service (no [meta_service].domains) does not list domains.
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (typed) navigate(`/d/${encodeURIComponent(typed)}/builds`);
      }}
    >
      <label>
        Domain
        <input value={typed} onChange={(e) => setTyped(e.target.value)} size={14} />
      </label>
    </form>
  );
}

export function Layout() {
  const me = useAsync(() => must(api.GET("/api/v1/whoami")), []);
  const domain = useDomain();
  const [theme, setTheme] = useTheme();
  const d = domain ? `/d/${encodeURIComponent(domain)}` : null;
  return (
    <Me.Provider value={me.status === "ok" ? me.value : null}>
      <a
        className="skip"
        href="#main"
        onClick={(e) => {
          e.preventDefault(); // hash routes: focus, do not navigate
          document.getElementById("main")?.focus();
        }}
      >
        Skip to content
      </a>
      <header className="top">
        <span className="brand">EBS</span>
        {d && (
          <nav aria-label="Domain pages">
            <ul>
              <li><NavLink to={`${d}/builds`}>Builds</NavLink></li>
              <li><NavLink to={`${d}/metrics`}>Metrics</NavLink></li>
              <li><NavLink to={`${d}/releases`}>Releases</NavLink></li>
              <li><NavLink to={`${d}/provenance`}>Provenance</NavLink></li>
            </ul>
          </nav>
        )}
        <span className="spacer" />
        {me.status === "ok" && <DomainPicker me={me.value} />}
        <label>
          Theme
          <select value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
            <option value="system">system</option>
            <option value="light">light</option>
            <option value="dark">dark</option>
          </select>
        </label>
        {me.status === "ok" && <span className="muted">{me.value.name}</span>}
      </header>
      <main id="main" tabIndex={-1}>
        {me.status === "error" ? <ErrorBox error={me.error} /> : <Outlet />}
      </main>
    </Me.Provider>
  );
}

export function Home() {
  const me = useMe();
  if (!me) return <p role="status">Loading…</p>;
  return (
    <>
      <h1>EBS dashboard</h1>
      {me.domains && me.domains.length === 0 ? (
        <p>You are not a member of any domain this service serves. Ask the domain admins for access.</p>
      ) : (
        <ul>
          {(me.domains ?? []).map((d) => (
            <li key={d}>
              <NavLink to={`/d/${encodeURIComponent(d)}/builds`}>{d}</NavLink>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
