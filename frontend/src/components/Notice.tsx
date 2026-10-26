import type { ReactNode } from "react";
import type { ApiError } from "../api/client";

type Tone = "info" | "warn" | "bad" | "good";

export function Notice({ tone = "info", title, children, role }: {
  tone?: Tone;
  title: string;
  children?: ReactNode;
  role?: "alert" | "status";
}) {
  return (
    <div className={`notice notice--${tone}`} role={role}>
      <p className="notice__title">{title}</p>
      {children && <div className="notice__body">{children}</div>}
    </div>
  );
}

const ERROR_TITLE: Record<ApiError["kind"], string> = {
  network: "Backend unreachable",
  timeout: "Backend timed out",
  http: "Request rejected",
  malformed: "Unreadable response",
};

export function ErrorNotice({ error, onRetry, title }: { error: ApiError; onRetry?: () => void; title?: string }) {
  return (
    <Notice tone="bad" role="alert" title={title ?? ERROR_TITLE[error.kind]}>
      <p>
        {error.message}
        {error.status ? <span className="mono muted"> (HTTP {error.status})</span> : null}
      </p>
      {onRetry && (
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRetry}>
          Try again
        </button>
      )}
    </Notice>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p className="loading" role="status">
      <span className="loading__bar" aria-hidden="true" />
      {label}
    </p>
  );
}
