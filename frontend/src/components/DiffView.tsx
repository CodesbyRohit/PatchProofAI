/** Renders a unified diff. The +/- prefixes stay visible, so meaning never depends on color alone. */
export function DiffView({ diff, label }: { diff: string; label: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff" aria-label={label} tabIndex={0}>
      <code>
        {lines.map((line, i) => {
          let kind = "ctx";
          if (line.startsWith("+++") || line.startsWith("---")) kind = "file";
          else if (line.startsWith("@@")) kind = "hunk";
          else if (line.startsWith("+")) kind = "add";
          else if (line.startsWith("-")) kind = "del";
          return (
            <span key={i} className={`diff__line diff__line--${kind}`}>
              {line || " "}
              {"\n"}
            </span>
          );
        })}
      </code>
    </pre>
  );
}
