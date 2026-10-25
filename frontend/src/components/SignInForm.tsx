"use client";
import { useState } from "react";
import { api, hardNavigate, problemText } from "@/lib/api-client";
import type { Principal } from "@/lib/session";

type Text = Record<string, string>;

export function SignInForm({ t, errors }: { t: Text; errors: Text }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; ref: string } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api<Principal>("/auth/login", { method: "POST", body: { username, password } });
    if (r.ok) {
      // the account's saved language wins; a full navigation lets the server pages see the new cookie
      const p = r.data;
      hardNavigate(p.must_change_password ? `/${p.language}/change-password` : `/${p.language}`);
      return;
    }
    const p = r.problem;
    const text =
      p.code === "TOO_MANY_ATTEMPTS"
        ? t.throttled.replace("{minutes}", String(Math.max(1, Math.ceil((p.retryAfterS ?? 60) / 60))))
        : p.code === "UNAUTHENTICATED"
          ? t.failed
          : problemText(errors, p);
    setError({ text, ref: p.requestId });
    setBusy(false);
  }

  return (
    <form onSubmit={submit} className="form" noValidate>
      <label>
        {t.username}
        <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required value={username} onChange={(e) => setUsername(e.target.value)} />
      </label>
      <label>
        {t.password}
        <input name="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>
      {error && (
        <p role="alert" className="issue">
          {error.text}
          {error.ref && (
            <span className="muted">
              {" "}
              ({errors.reference}: {error.ref})
            </span>
          )}
        </p>
      )}
      <button type="submit" className="btn" disabled={busy || !username || !password}>
        {busy ? t.submitting : t.submit}
      </button>
    </form>
  );
}
