// Lighthouse budgets for the built frontend (served with Vercel's routing rules, no backend connected).
// Mobile runs use real DevTools throttling: simulated throttling loads the page unthrottled, so a web font
// arriving late (the layout shift this guards against) would never show up.
// Writes out/lighthouse.md (a summary table) and one JSON report per run.
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as chromeLauncher from "chrome-launcher";
import lighthouse from "lighthouse";
import desktopConfig from "lighthouse/core/config/desktop-config.js";
import { startServer } from "./serve.mjs";

const ROUTES = ["/", "/history", "/runs/run_0123abcd"];
// Pages robots.txt deliberately keeps out of search (they only show data from the API).
const NOT_INDEXED = new Set(["/history", "/runs/run_0123abcd"]);
const BUDGET = {
  cls: 0.05,
  // Measured locally with these settings (see the README); CI machines vary, so performance gets headroom.
  performance: { mobile: 0.6, desktop: 0.9 },
};
const out = resolve(fileURLToPath(new URL(".", import.meta.url)), "out");
mkdirSync(out, { recursive: true });

const server = await startServer();
const base = `http://127.0.0.1:${server.address().port}`;
const chrome = await chromeLauncher.launch({
  chromePath: process.env.CHROME_PATH || undefined,
  chromeFlags: ["--headless=new", "--no-sandbox"],
});
const rows = [];
let failures = 0;

function expect(ok, where, what) {
  if (!ok) {
    failures += 1;
    console.log(`FAIL  ${where}: ${what}`);
  }
}

// Performance is noisy on a shared machine (the same build scored 85 to 100 across identical runs), so each page is
// measured RUNS times and the MEDIAN performance is held to the budget: one slow outlier cannot fail the build, but a
// real regression slows most runs and still does. Everything that is deterministic stays strict on EVERY run:
// layout shift (the worst run counts), accessibility, agentic browsing, and each best-practices and SEO audit.
const RUNS = Number(process.env.LIGHTHOUSE_RUNS ?? 3);
const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];

for (const route of ROUTES) {
  for (const formFactor of ["mobile", "desktop"]) {
    const flags = { port: chrome.port, output: "json", logLevel: "error", throttlingMethod: "devtools" };
    const lhrs = [];
    for (let i = 0; i < RUNS; i++) {
      const { lhr } = await lighthouse(base + route, flags, formFactor === "desktop" ? desktopConfig : undefined);
      writeFileSync(resolve(out, `${route.replace(/\W+/g, "_") || "home"}-${formFactor}-${i + 1}.json`), JSON.stringify(lhr));
      lhrs.push(lhr);
    }
    const where = `${route} ${formFactor}`;
    const scores = (id) => lhrs.map((l) => l.categories[id]?.score ?? 0);
    const numbers = (id) => lhrs.map((l) => l.audits[id].numericValue);
    const perf = scores("performance");
    const cls = Math.max(...numbers("cumulative-layout-shift"));

    expect(cls <= BUDGET.cls, where, `CLS ${cls.toFixed(3)} > ${BUDGET.cls} (worst of ${RUNS} runs)`);
    expect(median(perf) >= BUDGET.performance[formFactor], where,
      `median performance ${Math.round(median(perf) * 100)} < ${BUDGET.performance[formFactor] * 100} (runs: ${perf.map((x) => Math.round(x * 100)).join(", ")})`);
    expect(Math.min(...scores("accessibility")) === 1, where, "accessibility below 100 in at least one run");
    expect(Math.min(...scores("agentic-browsing")) === 1, where, "agentic browsing below 100 in at least one run");

    // Best practices and SEO: every audit passes in every run, with two explained exceptions.
    lhrs.forEach((lhr, run) => {
      const audit = (id) => lhr.audits[id];
      const failing = (category) => lhr.categories[category].auditRefs
        .map((ref) => audit(ref.id))
        .filter((a) => a.score !== null && a.score < 1 && !["informative", "manual", "notApplicable"].includes(a.scoreDisplayMode));
      for (const a of failing("best-practices")) {
        if (a.id === "errors-in-console") {
          // Without a backend, /api requests fail and Chrome logs them. Anything else is a real error.
          const urlOf = (i) => i.sourceLocation?.url ?? i.url;
          const other = a.details.items.filter((i) => !(urlOf(i) && new URL(urlOf(i)).pathname.startsWith("/api/")));
          expect(other.length === 0, where, `run ${run + 1}: console errors not caused by the absent API: ${JSON.stringify(other)}`);
        } else {
          expect(false, where, `run ${run + 1}: best practices: ${a.id} (${a.title})`);
        }
      }
      for (const a of failing("seo")) {
        expect(a.id === "is-crawlable" && NOT_INDEXED.has(route), where, `run ${run + 1}: SEO: ${a.id} (${a.title})`);
      }
    });

    rows.push({
      where,
      perf: `${Math.round(median(perf) * 100)} (${perf.map((x) => Math.round(x * 100)).join("/")})`,
      a11y: Math.round(Math.min(...scores("accessibility")) * 100),
      bp: Math.round(Math.min(...scores("best-practices")) * 100),
      seo: Math.round(Math.min(...scores("seo")) * 100),
      agentic: Math.round(Math.min(...scores("agentic-browsing")) * 100),
      cls: cls.toFixed(3),
      lcp: Math.round(median(numbers("largest-contentful-paint"))),
      tbt: Math.round(median(numbers("total-blocking-time"))),
    });
  }
}

await chrome.kill();
server.close();

const table = [
  "| Page | Perf: median (runs) | A11y | BP | SEO | Agentic | CLS | LCP ms | TBT ms |",
  "|---|---|---|---|---|---|---|---|---|",
  ...rows.map((r) => `| ${r.where} | ${r.perf} | ${r.a11y} | ${r.bp} | ${r.seo} | ${r.agentic} | ${r.cls} | ${r.lcp} | ${r.tbt} |`),
].join("\n");
writeFileSync(resolve(out, "lighthouse.md"), `### Lighthouse (DevTools throttling, no backend connected; median of ${RUNS} runs, worst CLS)\n\n${table}\n`);
console.log(table);
console.log(failures ? `\n${failures} budget(s) failed.` : "\nAll Lighthouse budgets met.");
process.exit(failures ? 1 : 0);
