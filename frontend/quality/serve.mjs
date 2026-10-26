// Serve frontend/dist the way Vercel does: vercel.json is compiled by Vercel's own routing package
// (@vercel/routing-utils), then requests go through the same phases as a deployment:
// header routes -> existing static files -> rewrites (which must land on a file) -> 404.
// Used by the quality checks; run directly with `node serve.mjs [port]` to browse the build.
import { readFileSync, statSync, createReadStream } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { createBrotliCompress, createGzip } from "node:zlib";
import { getTransformedRoutes } from "@vercel/routing-utils";

const here = fileURLToPath(new URL(".", import.meta.url));
export const DIST = resolve(process.env.QUALITY_DIST ?? resolve(here, "..", "dist"));
const config = JSON.parse(readFileSync(resolve(here, "..", "vercel.json"), "utf8"));

const { routes, error } = getTransformedRoutes({ rewrites: config.rewrites, headers: config.headers });
if (error) throw new Error(`vercel.json does not compile: ${error.message}`);
const filesystemAt = routes.findIndex((r) => r.handle === "filesystem");
const headerRoutes = routes.slice(0, filesystemAt).filter((r) => r.continue);
const rewriteRoutes = routes.slice(filesystemAt + 1).filter((r) => r.src && r.dest);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

function fileFor(path) {
  let decoded;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return null;
  }
  const full = normalize(join(DIST, decoded));
  if (full !== DIST && !full.startsWith(DIST + sep)) return null;
  try {
    return statSync(full).isFile() ? full : null;
  } catch {
    return null;
  }
}

/** Resolve a path like Vercel: {status, file?, headers}. */
export function resolveRequest(path) {
  const headers = {};
  for (const r of headerRoutes) if (new RegExp(r.src).test(path)) Object.assign(headers, r.headers);
  const direct = fileFor(path);
  if (direct) return { status: 200, file: direct, headers };
  for (const r of rewriteRoutes) {
    const match = new RegExp(r.src).exec(path);
    if (!match) continue;
    const dest = r.dest.replace(/\$(\d+)/g, (_, i) => match[Number(i)] ?? "");
    const target = fileFor(dest);
    if (target) return { status: 200, file: target, headers };
  }
  return { status: 404, headers };
}

export function startServer(port = 0) {
  const server = createServer((req, res) => {
    const path = new URL(req.url ?? "/", "http://localhost").pathname;
    const { status, file, headers } = resolveRequest(path);
    for (const [k, v] of Object.entries(headers)) res.setHeader(k, v);
    if (!file) {
      res.writeHead(status, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("The page could not be found\n\nNOT_FOUND\n");
      return;
    }
    const type = TYPES[extname(file)] ?? "application/octet-stream";
    // Vercel compresses text responses; without this, local transfer sizes (and LCP) would be ~3x worse.
    const accepts = String(req.headers["accept-encoding"] ?? "");
    const compressible = /^(text\/|application\/(json|xml)|image\/svg)/.test(type);
    if (compressible && /\bbr\b/.test(accepts)) {
      res.writeHead(200, { "Content-Type": type, "Content-Encoding": "br", Vary: "Accept-Encoding" });
      createReadStream(file).pipe(createBrotliCompress()).pipe(res);
    } else if (compressible && /\bgzip\b/.test(accepts)) {
      res.writeHead(200, { "Content-Type": type, "Content-Encoding": "gzip", Vary: "Accept-Encoding" });
      createReadStream(file).pipe(createGzip()).pipe(res);
    } else {
      res.writeHead(200, { "Content-Type": type });
      createReadStream(file).pipe(res);
    }
  });
  return new Promise((ok) => server.listen(port, "127.0.0.1", () => ok(server)));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = await startServer(Number(process.argv[2] ?? 4175));
  console.log(`Serving ${DIST} with Vercel routing on http://127.0.0.1:${server.address().port}`);
}
