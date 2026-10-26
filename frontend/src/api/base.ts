/**
 * Where the PatchProof API lives, from VITE_API_BASE_URL at build time.
 *
 * Unset or empty: the same origin as the page (`/api/...`), which covers the Vite dev proxy, the backend
 * serving the build itself, and a Vercel rewrite of /api to the backend. Otherwise an absolute http(s) URL
 * such as `https://api.example.com` (an optional path prefix is kept); the backend must then list the
 * frontend's origin in PATCHPROOF_CORS_ORIGINS. VITE_* values are public: this is an address, never a secret.
 */
export function parseApiBase(raw: string | undefined, { production = false } = {}): string {
  const value = (raw ?? "").trim();
  if (!value) return "";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`VITE_API_BASE_URL must be an absolute URL such as https://api.example.com, not "${value}".`);
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error(`VITE_API_BASE_URL must use https:// (or http:// for local development), not "${value}".`);
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error(`VITE_API_BASE_URL must not contain credentials, a query or a fragment: "${value}".`);
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (production && url.protocol === "http:" && !local) {
    throw new Error(`VITE_API_BASE_URL must use https:// in a production build; browsers block http:// API calls from an https:// page: "${value}".`);
  }
  return url.origin + url.pathname.replace(/\/+$/, "");
}

/** Join the base with an API path (which always starts with "/api/"). */
export function joinApi(base: string, path: string): string {
  return base + path;
}
