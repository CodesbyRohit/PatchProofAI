import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from "react";

// A tiny History-API router. The backend (or, on Vercel, the rewrite in vercel.json) serves index.html for
// unknown non-API paths, so deep links work.

const NAV_EVENT = "patchproof:navigate";

function subscribe(cb: () => void) {
  window.addEventListener("popstate", cb);
  window.addEventListener(NAV_EVENT, cb);
  return () => {
    window.removeEventListener("popstate", cb);
    window.removeEventListener(NAV_EVENT, cb);
  };
}

export function navigate(to: string) {
  if (to === window.location.pathname) return;
  window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(NAV_EVENT));
  window.scrollTo({ top: 0 });
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, () => window.location.pathname);
}

export type Route =
  | { name: "workspace" }
  | { name: "history" }
  | { name: "github" }
  | { name: "run"; id: string }
  | { name: "missing" };

export function matchRoute(path: string): Route {
  if (path === "/" || path === "") return { name: "workspace" };
  if (path === "/history") return { name: "history" };
  if (path === "/github") return { name: "github" };
  const m = /^\/runs\/([A-Za-z0-9_-]{1,64})\/?$/.exec(path);
  if (m?.[1]) return { name: "run", id: m[1] };
  return { name: "missing" };
}

export function Link({ to, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  function handle(e: MouseEvent<HTMLAnchorElement>) {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(to);
  }
  return <a href={to} onClick={handle} {...rest} />;
}
