import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import { parseApiBase } from "./src/api/base";

// The browser only ever talks to the PatchProof backend. The BharatCode key lives there, never here:
// nothing in this config (or any VITE_* variable) carries a secret.
const backend = process.env.PATCHPROOF_BACKEND_URL ?? "http://127.0.0.1:8000";

// Latin fonts used above the fold. Without a preload the browser only requests a font once text using it
// is rendered, so the first paint uses fallback fonts and the page reflows when they arrive (layout shift).
const PRELOADED_FONTS = [
  "instrument-serif-latin-400-normal-",
  "instrument-serif-latin-400-italic-",
  "ibm-plex-sans-latin-400-normal-",
];

/** Add <link rel="preload"> for the hashed font files Vite emits. */
function preloadFonts(prefixes: string[]): Plugin {
  return {
    name: "patchproof:preload-fonts",
    apply: "build",
    transformIndexHtml: {
      order: "post",
      handler(_html, ctx) {
        const files = Object.keys(ctx.bundle ?? {}).filter(
          (f) => f.endsWith(".woff2") && prefixes.some((p) => f.split("/").pop()!.startsWith(p)),
        );
        if (files.length !== prefixes.length) {
          throw new Error(`Expected ${prefixes.length} preloaded fonts, found ${files.length}: ${files.join(", ")}`);
        }
        return files.map((f) => ({
          tag: "link",
          attrs: { rel: "preload", href: `/${f}`, as: "font", type: "font/woff2", crossorigin: "" },
          injectTo: "head" as const,
        }));
      },
    },
  };
}

export default defineConfig(({ command, mode }) => {
  // Fail the build (or dev server) on a malformed API address instead of shipping a frontend that calls
  // the wrong place. Unset means same origin: see src/api/base.ts.
  parseApiBase(loadEnv(mode, process.cwd(), "VITE_").VITE_API_BASE_URL, { production: command === "build" && mode === "production" });
  return {
    plugins: [react(), preloadFonts(PRELOADED_FONTS)],
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
