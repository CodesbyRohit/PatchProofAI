import { createContext, useContext, useEffect, type ReactNode } from "react";
import { auth, setCsrfToken, type ApiError } from "../api/client";
import type { Me } from "../api/types";
import { useAsync } from "./hooks";

interface Session {
  me: Me | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

const SessionContext = createContext<Session>({ me: null, error: null, loading: true, reload: () => {} });

/** Who is signed in (GitHub, access token, or open local use), fetched once for the whole app. */
export function SessionProvider({ children }: { children: ReactNode }) {
  const state = useAsync("me", () => auth.me());
  const csrf = state.data?.csrf ?? null;
  useEffect(() => setCsrfToken(csrf), [csrf]);
  return (
    <SessionContext.Provider value={{ me: state.data, error: state.error, loading: state.loading, reload: state.reload }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): Session {
  return useContext(SessionContext);
}
