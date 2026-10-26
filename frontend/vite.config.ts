import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import { parseApiBase } from "./src/api/base";

// The browser only ever talks to the PatchProof backend. The BharatCode key lives there, never here:
// nothing in this config (or any VITE_* variable) carries a secret.
const backend = process.env.PATCHPROOF_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig(({ command, mode }) => {
  // Fail the build (or dev server) on a malformed API address instead of shipping a frontend that calls
  // the wrong place. Unset means same origin: see src/api/base.ts.
  parseApiBase(loadEnv(mode, process.cwd(), "VITE_").VITE_API_BASE_URL, { production: command === "build" && mode === "production" });
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: { "/api": { target: backend, changeOrigin: false } },
    },
    build: { outDir: "dist", sourcemap: true },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
