// Browser journey: sign-in, repository, pinned commit, verified run in Docker, draft pull request, another user
// refused, sign-out. GitHub's consent page is the only thing intercepted (the real /start response is fetched
// and only its Location, which would go to github.com, is answered the way GitHub would: back with a code).
// Usage: node journey.mjs [base-url]      (start harness.py first; build frontend/dist first)
import { chromium } from "playwright-core";

const APP = process.argv[2] ?? "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH
  ?? (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "/usr/bin/google-chrome");
const FIX = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n";
let failed = 0;
const log = (ok, what) => { console.log(`${ok ? "PASS" : "FAIL"}  ${what}`); if (!ok) failed += 1; };

async function context(browser, code) {
  const ctx = await browser.newContext();
  await ctx.route(`${APP}/api/auth/github/start`, async (route) => {
    const response = await route.fetch({ maxRedirects: 0 });
    const location = new URL(response.headers()["location"]);
    const ok = location.hostname === "github.com" && location.searchParams.get("code_challenge_method") === "S256";
    await route.fulfill({ response, headers: { ...response.headers(),
      location: `${APP}/api/auth/github/callback?code=${ok ? code : "missing-pkce"}&state=${location.searchParams.get("state")}` } });
  });
  await ctx.route("https://github.com/**", (route) => route.abort());
  return ctx;
}

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
try {
  const page = await (await context(browser, "code-alice")).newPage();
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto(`${APP}/github`);
  await page.getByRole("link", { name: "Sign in with GitHub" }).click();
  await page.getByText("@alice", { exact: true }).first().waitFor({ timeout: 15000 });
  log(page.url() === `${APP}/github`, "signed in through the OAuth redirect (state + PKCE)");

  await page.getByRole("button", { name: "Show repositories" }).click();
  await page.getByRole("button", { name: /alice\/private-app/ }).click();
  await page.getByTitle("a".repeat(40)).waitFor({ timeout: 15000 });
  log(true, "branch resolved to the pinned commit");
  await page.getByRole("radio", { name: "Verify my own diff" }).check();
  await page.getByLabel("Unified diff").fill(FIX);
  await page.getByRole("button", { name: "Start verification" }).click();
  await page.waitForURL(/\/runs\/run_[0-9a-f]+$/, { timeout: 15000 });
  const runUrl = page.url();
  await page.getByRole("img", { name: "Verdict: Verified for tested cases" }).waitFor({ timeout: 240000 });
  log(true, "private repository verified in the Docker sandbox");
  log(await page.getByRole("region", { name: "Source revision" }).getByText("alice/private-app").isVisible(), "run page shows the source commit");

  await page.getByRole("button", { name: "Prepare a draft pull request" }).click();
  await page.getByLabel("Patch to be committed").waitFor();
  log(await page.getByRole("button", { name: "Create draft pull request" }).isDisabled(), "pull request is blocked until the patch is reviewed");
  await page.getByRole("checkbox", { name: "I reviewed this patch" }).check();
  await page.getByRole("button", { name: "Create draft pull request" }).click();
  const link = page.getByRole("link", { name: /Open pull request #\d+/ });
  await link.waitFor({ timeout: 30000 });
  log((await link.getAttribute("href")).endsWith("/alice/private-app/pull/1"), "draft pull request created");

  const bob = await (await context(browser, "code-bob")).newPage();
  await bob.goto(`${APP}/github`);
  await bob.getByRole("link", { name: "Sign in with GitHub" }).click();
  await bob.getByText("@bob", { exact: true }).first().waitFor({ timeout: 15000 });
  await bob.goto(runUrl);
  await bob.getByText("Run not found").first().waitFor({ timeout: 15000 });
  log(true, "another GitHub user cannot open the run");

  await page.goto(`${APP}/github`);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("link", { name: "Sign in with GitHub" }).waitFor({ timeout: 15000 });
  log(true, "sign-out returns to the sign-in screen");
  log(errors.length === 0, `no console errors ${errors.length ? JSON.stringify(errors) : ""}`);
} catch (e) {
  console.error("FAIL ", e.message);
  failed += 1;
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
