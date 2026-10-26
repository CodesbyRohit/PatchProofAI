import type { PatchSource } from "../api/types";
import { SOURCE_LABEL } from "../lib/format";

export function SourceBadge({ source }: { source: PatchSource }) {
  const label = SOURCE_LABEL[source];
  return (
    <span className={`source source--${source}`} title={label.long}>
      {label.short}
    </span>
  );
}
