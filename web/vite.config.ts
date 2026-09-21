import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The SPA is served as static files; the API lives on the same origin under /api and
// /healthz. In dev, proxy those to the FastAPI process (ADR-0005).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
  build: {
    sourcemap: false,
    target: "es2022",
  },
});
