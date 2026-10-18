/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dashboard is served by the metadata service at /ui/ (`[meta_service].dashboard_dir`), so
// the API and its session cookie are same-origin. `npm run dev` proxies to EBS_META_URL.
const meta = process.env.EBS_META_URL ?? "http://localhost:8000";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: meta, changeOrigin: false },
      "/auth": { target: meta, changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: true },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
