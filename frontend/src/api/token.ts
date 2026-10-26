import { useSyncExternalStore } from "react";

// The access token for a hosted PatchProof instance. The user types it in; it is never part of the build.
// It is kept in sessionStorage (this tab only, gone when the tab closes), with an in-memory fallback when
// storage is unavailable.

const KEY = "patchproof.accessToken";
let memory: string | null = null;
let version = 0;
const listeners = new Set<() => void>();

export function getToken(): string | null {
  try {
    return window.sessionStorage.getItem(KEY) ?? memory;
  } catch {
    return memory;
  }
}

function changed() {
  version += 1;
  listeners.forEach((cb) => cb());
}

export function setToken(token: string) {
  memory = token;
  try {
    window.sessionStorage.setItem(KEY, token);
  } catch {
    // storage blocked: the in-memory copy lasts until the page is reloaded
  }
  changed();
}

export function clearToken() {
  memory = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // nothing stored
  }
  changed();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** Changes whenever the token is set or cleared, so data hooks can load again with it. */
export function useTokenVersion(): number {
  return useSyncExternalStore(subscribe, () => version);
}
