// Routing and discovery checks against the built frontend, served with Vercel's routing rules.
// Fails (exit 1) if a client route stops working, a missing file or API path falls through to the app
// shell, or a discovery file breaks the rules Lighthouse 13.5 applies to it.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DIST, startServer } from "./serve.mjs";

const server = await startServer();
const base = `http://127.0.0.1:${server.address().port}`;
let failures = 0;

function check(ok, what) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${what}`);
  if (!ok) failures += 1;
}

async function get(path) {
  const res = await fetch(base + path);
  return { status: res.status, type: res.headers.get("content-type") ?? "", body: await res.text(), headers: res.headers };
}

const shell = readFileSync(resolve(DIST, "index.html"), "utf8");
const asset = shell.match(/src="(\/assets\/[^"]+\.js)"/)?.[1];

// Client routes and real files.
for (const path of ["/", "/history", "/runs/run_0123abcd", "/runs/run_0123abcd/", "/no/such/page"]) {
  const r = await get(path);
  check(r.status === 200 && r.type.startsWith("text/html") && r.body === shell, `${path} serves the app shell`);
}
for (const [path, type] of [[asset, "text/javascript"], ["/favicon.svg", "image/svg+xml"]]) {
  const r = await get(path);
  check(r.status === 200 && r.type.startsWith(type), `${path} is served as ${type}`);
}

// Nothing missing may turn into the app shell with a 200.
for (const path of [
  "/assets/missing.js", "/assets/missing.css", "/missing.png", "/data.json", "/manifest.webmanifest",
  "/.well-known/ai-catalog.json", "/.well-known/ard.json", "/.well-known/security.txt",
  "/api", "/api/", "/api/status", "/api/runs/run_1/report.md",
]) {
  const r = await get(path);
  check(r.status === 404 && !r.type.startsWith("text/html"), `${path} is a non-HTML 404 (got ${r.status} ${r.type})`);
}

// Security headers on every response.
const home = await get("/");
check(home.headers.get("x-content-type-options") === "nosniff", "X-Content-Type-Options: nosniff is set");
check((home.headers.get("content-security-policy") ?? "").includes("frame-ancestors 'none'"), "pages cannot be framed");

// llms.txt: the rules of Lighthouse's llms-txt audit, plus every link must be an absolute https URL.
const llms = await get("/llms.txt");
check(llms.status === 200 && llms.type.startsWith("text/plain"), "/llms.txt is served as text/plain");
check(/^\s*#\s+.+/m.test(llms.body), "/llms.txt starts sections with a Markdown H1");
const links = [...llms.body.matchAll(/\[([^\]]+)\]\(([^)]+)\)/g)].map((m) => m[2]);
check(links.length > 0 && llms.body.length >= 50, `/llms.txt has Markdown links (${links.length}) and real content`);
check(links.every((l) => l.startsWith("https://")), "/llms.txt links are absolute https URLs");

// robots.txt and sitemap.xml.
const robots = await get("/robots.txt");
check(robots.status === 200 && robots.type.startsWith("text/plain") && /^User-agent: \*/m.test(robots.body),
  "/robots.txt is a real robots file");
const sitemapUrl = robots.body.match(/^Sitemap:\s*(\S+)/m)?.[1];
check(Boolean(sitemapUrl?.startsWith("https://")), "/robots.txt names an absolute sitemap URL");
const sitemap = await get("/sitemap.xml");
check(sitemap.status === 200 && sitemap.body.includes("<urlset") && !sitemap.body.includes("/runs/"),
  "/sitemap.xml lists public pages only");

// The links we publish must exist (network; skip with QUALITY_OFFLINE=1).
if (process.env.QUALITY_OFFLINE !== "1") {
  for (const url of links) {
    let status = 0;
    for (let attempt = 0; attempt < 3 && status !== 200; attempt++) {
      status = await fetch(url, { method: "GET", redirect: "follow" }).then((r) => r.status, () => 0);
    }
    check(status === 200, `llms.txt link resolves: ${url} (${status})`);
  }
}

server.close();
console.log(failures ? `\n${failures} check(s) failed.` : "\nAll routing and discovery checks passed.");
process.exit(failures ? 1 : 0);
