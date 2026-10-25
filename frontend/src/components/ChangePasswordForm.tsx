"use client";
import { useState } from "react";
import { api, hardNavigate, problemText } from "@/lib/api-client";

type Text = Record<string, string>;

export function ChangePasswordForm({ lang, csrf, t, errors }: { lang: string; csrf: string; t: Text; errors: Text }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; ref: string } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (next.length < 10) return setError({ text: t.tooShort, ref: "" });
    if (next !== again) return setError({ text: t.mismatch, ref: "" });
    setBusy(true);
    const r = await api("/auth/change-password", { method: "POST", body: { current_password: current, new_password: next }, csrf });
    if (r.ok) {
      hardNavigate(`/${lang}`);
      return;
    }
    const p = r.problem;
    const text = p.issueCode === "WEAK_PASSWORD" ? t.weak : p.code === "UNAUTHENTICATED" ? t.wrongCurrent : problemText(errors, p);
    setError({ text, ref: p.requestId });
    setBusy(false);
  }

  return (
    <form onSubmit={submit} className="form" noValidate>
      <label>
        {t.current}
        <input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
      </label>
      <label>
        {t.new}
        <input type="password" autoComplete="new-password" required minLength={10} value={next} onChange={(e) => setNext(e.target.value)} />
        <span className="muted">{t.hint}</span>
      </label>
      <label>
        {t.confirm}
        <input type="password" autoComplete="new-password" required value={again} onChange={(e) => setAgain(e.target.value)} />
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
      <button type="submit" className="btn" disabled={busy || !current || !next || !again}>
        {busy ? t.saving : t.submit}
      </button>
    </form>
  );
}
