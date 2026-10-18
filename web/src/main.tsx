import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createHashRouter, RouterProvider } from "react-router-dom";
import { Home, Layout } from "./components/Layout";
import { BuildPage } from "./pages/Build";
import { BuildsPage } from "./pages/Builds";
import { LogPage } from "./pages/Log";
import { MetricsPage } from "./pages/Metrics";
import { ProvenancePage } from "./pages/Provenance";
import { RegressionPage } from "./pages/Regression";
import { ReleasesPage } from "./pages/Releases";
import "./styles.css";

// Hash routes: the service serves the static build at /ui/ without a fallback, so a deep link
// (/ui/#/builds/42) always loads index.html.
const router = createHashRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <Home /> },
      { path: "/d/:domain/builds", element: <BuildsPage /> },
      { path: "/d/:domain/metrics", element: <MetricsPage /> },
      { path: "/d/:domain/releases", element: <ReleasesPage /> },
      { path: "/d/:domain/provenance", element: <ProvenancePage /> },
      { path: "/d/:domain/builds/:build", element: <BuildPage /> },
      { path: "/d/:domain/builds/:build/regression", element: <RegressionPage /> },
      { path: "/d/:domain/builds/:build/log/*", element: <LogPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
