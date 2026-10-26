import { useEffect } from "react";
import { History } from "./pages/History";
import { RunPage } from "./pages/RunPage";
import { Workspace } from "./pages/Workspace";
import { Link, matchRoute, usePath } from "./router";

function Masthead({ active }: { active: string }) {
  return (
    <header className="masthead">
      {/* The accessible name is the visible text ("PatchProof AI"), so voice control can say what it sees. */}
      <Link to="/" className="wordmark">
        <span className="wordmark__mark" aria-hidden="true" />
        <span>PatchProof</span> <span className="wordmark__ai">AI</span>
      </Link>
      <nav aria-label="Primary">
        <ul className="nav">
          <li><Link to="/" aria-current={active === "workspace" ? "page" : undefined}>Workspace</Link></li>
          <li><Link to="/history" aria-current={active === "history" ? "page" : undefined}>History</Link></li>
        </ul>
      </nav>
    </header>
  );
}

const TITLES: Record<string, string> = {
  workspace: "PatchProof AI: evidence before you trust a patch",
  history: "Run history · PatchProof AI",
  missing: "Not found · PatchProof AI",
};

export function App() {
  const route = matchRoute(usePath());
  const title = route.name === "run" ? `Run ${route.id} · PatchProof AI` : (TITLES[route.name] ?? "PatchProof AI");
  useEffect(() => {
    document.title = title;
  }, [title]);
  return (
    <>
      <a href="#main" className="skip">Skip to content</a>
      <Masthead active={route.name} />
      <main id="main">
        {route.name === "workspace" && <Workspace />}
        {route.name === "history" && <History />}
        {route.name === "run" && <RunPage key={route.id} id={route.id} />}
        {route.name === "missing" && (
          <div className="page">
            <h1 className="page__title">Nothing here</h1>
            <p><Link to="/">Back to the workspace</Link></p>
          </div>
        )}
      </main>
      <footer className="footer small muted">
        <span>PatchProof AI · Team LoneWolf · BharatCode Build League</span>
        <span>Verdicts are decided by deterministic rules over observed test results.</span>
      </footer>
    </>
  );
}
