import { useId, useState, type FormEvent } from "react";
import { getToken, setToken } from "../api/token";

/** Asks for the access token of a hosted instance. Saving it reloads every view that needs the API. */
export function AccessForm() {
  const id = useId();
  const [value, setValue] = useState("");
  const rejected = getToken() !== null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const token = value.trim();
    if (token) setToken(token);
  }

  return (
    <form className="access-form" onSubmit={submit}>
      <label htmlFor={`${id}-token`} className="field__label">
        {rejected ? "Enter a valid access token" : "Access token"}
      </label>
      <div className="access-form__row">
        <input
          id={`${id}-token`}
          className="field mono"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-describedby={`${id}-hint`}
        />
        <button type="submit" className="btn btn--primary" disabled={!value.trim()}>
          Use token
        </button>
      </div>
      <p id={`${id}-hint`} className="small muted">
        Ask the operator of this PatchProof instance for a token. It is kept only in this browser tab.
      </p>
    </form>
  );
}
