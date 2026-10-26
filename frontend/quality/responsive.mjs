// Responsive checks of the built frontend in a real Chrome (no browser download: set CHROME_PATH or use a
// default install). For every route at every viewport: no horizontal page overflow, interactive controls at
// least 24x24 px on touch-sized screens (WCAG 2.5.8), and no console errors other than the API being absent.
// Screenshots go to quality/out/screens/ for review.
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { startServer } from "./serve.mjs";

const VIEWPORTS = [
  [320, 568], [360, 800], [390, 844], [430, 932], [768, 1024], [1024, 768], [1280, 800], [1440, 900], [1920, 1080],
];
// Two passes: as deployed today (no backend: every API call fails), and with real captured API responses
// (src/test/fixtures, recorded from the running API) so the ledger, diffs and logs are laid out too.
// The fixtures are served only to this test browser; nothing in the build uses them.
const fixtures = resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "src", "test", "fixtures");
const fixture = (name) => readFileSync(resolve(fixtures, `${name}.json`), "utf8");
const API_FIXTURES = {
  "/api/status": fixture("status"),
  "/api/examples": fixture("examples"),
  "/api/runs": fixture("runs"),
  ...Object.fromEntries(["plausible", "blocked", "inconclusive", "minimal", "plan_required"].map((n) => {
    const body = fixture(n);
    return [`/api/runs/${JSON.parse(body).id}`, body];
  })),
};
const runId = (name) => JSON.parse(fixture(name)).id;
const PASSES = [
  { name: "no backend", api: null, routes: ["/", "/history", "/runs/run_0123abcd", "/no/such/page"] },
  { name: "captured API", api: API_FIXTURES,
    routes: ["/", "/history", `/runs/${runId("plausible")}`, `/runs/${runId("blocked")}`, `/runs/${runId("inconclusive")}`, `/runs/${runId("plan_required")}`] },
];
const CHROME = process.env.CHROME_PATH
  ?? (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "/usr/bin/google-chrome");
const shots = resolve(fileURLToPath(new URL(".", import.meta.url)), "out", "screens");
mkdirSync(shots, { recursive: true });

const server = await startServer();
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: CHROME, headless: true });
let failures = 0;

let combinations = 0;
for (const pass of PASSES) for (const [width, height] of VIEWPORTS) {
  const context = await browser.newContext({ viewport: { width, height }, hasTouch: width < 768, isMobile: width < 768 });
  if (pass.api) {
    await context.route("**/api/**", (route) => {
      const body = pass.api[new URL(route.request().url()).pathname];
      return body
        ? route.fulfill({ status: 200, contentType: "application/json", body })
        : route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"Not found."}' });
    });
  }
  for (const route of pass.routes) {
    combinations += 1;
    const page = await context.newPage();
    const errors = [];
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    page.on("pageerror", (e) => errors.push(e.message));
    const apiFailures = [];
    page.on("response", (r) => new URL(r.url()).pathname.startsWith("/api/") && r.status() >= 400 && apiFailures.push(r.status()));
    await page.goto(base + route, { waitUntil: "networkidle" });
    await page.waitForTimeout(300); // let error states from the absent API render

    const result = await page.evaluate((touch) => {
      const doc = document.documentElement;
      const overflow = doc.scrollWidth - window.innerWidth;
      const wide = [...document.querySelectorAll("body *")]
        .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1 && getComputedStyle(el).position !== "fixed")
        .slice(0, 3).map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join(".")}`);
      // Inline links inside running text are exempt from the target-size rule; controls are not.
      const small = touch ? [...document.querySelectorAll("button, input, select, textarea, nav a, a.btn, a.wordmark")]
        .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && (r.width < 24 || r.height < 24); })
        .map((el) => `${el.tagName.toLowerCase()} "${(el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 30)}"`) : [];
      return { overflow, wide, small };
    }, width < 768);

    const where = `[${pass.name}] ${String(width).padStart(4)}x${height} ${route}`;
    // The API is absent here, so failed /api requests (and Chrome's matching console lines) are expected.
    const unexpected = errors.filter((e) => !/Failed to load resource: the server responded with a status of 404/.test(e));
    const problems = [];
    if (result.overflow > 1) problems.push(`horizontal overflow ${result.overflow}px (${result.wide.join(", ")})`);
    if (result.small.length) problems.push(`targets under 24px: ${result.small.join(", ")}`);
    if (unexpected.length) problems.push(`console errors: ${unexpected.join(" | ")}`);
    if (errors.length > apiFailures.length) problems.push("console errors not explained by API 404s");
    console.log(`${problems.length ? "FAIL" : "PASS"}  ${where}${problems.length ? "  " + problems.join("; ") : ""}`);
    failures += problems.length ? 1 : 0;
    const shot = `${pass.api ? "api" : "nobackend"}-${width}x${height}${route.replace(/\//g, "_")}.png`;
    await page.screenshot({ path: resolve(shots, shot), fullPage: true });
    await page.close();
  }
  await context.close();
}

await browser.close();
server.close();
console.log(failures ? `\n${failures} of ${combinations} viewport/route combination(s) failed.` : `\nAll ${combinations} viewport/route combinations passed.`);
process.exit(failures ? 1 : 0);
