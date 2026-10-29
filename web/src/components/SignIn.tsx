import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, signIn } from "../api";
import { Card } from "./Card";

// Shown when the Mac answers 401: this browser is not signed in, or the token
// was rotated. The token is sent once and never kept by the page; the Mac sets
// a cookie page code cannot read (H1, 2026-10-28).
export function SignIn() {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await signIn(token.trim());
      setToken("");
      window.location.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen bg-bg text-text">
      <main className="mx-auto w-full max-w-app px-4 pt-safe-16">
        <Card>
          <form onSubmit={submit}>
            <p className="font-serif text-xl">Sign in to Helios</p>
            <p className="mt-2 text-sm text-muted">
              Paste the token from the Mac (ingest_token in ~/Helios/helios.toml). This browser
              stays signed in until the token changes.
            </p>
            <label className="mt-4 block text-sm text-muted" htmlFor="helios-token-input">
              Token
            </label>
            <input
              id="helios-token-input"
              type="password"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className="mt-1 w-full rounded-xl border border-hairline bg-bg px-3 py-2 text-text"
            />
            {error ? (
              <p className="mt-3 text-sm" role="alert">
                {error}
              </p>
            ) : null}
            <button
              type="submit"
              disabled={busy || !token.trim()}
              className="mt-4 rounded-full border border-hairline px-4 py-2 text-sm text-text transition-colors hover:bg-hairline/40 disabled:opacity-50"
            >
              {busy ? "Signing in..." : "Sign in"}
            </button>
          </form>
        </Card>
      </main>
    </div>
  );
}
