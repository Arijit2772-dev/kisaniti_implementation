"use client";
import { useState } from "react";
import { api, hardNavigate } from "@/lib/api-client";

export function SignOutButton({ lang, csrf, label, errors }: { lang: string; csrf: string; label: string; errors: Record<string, string> }) {
  const [failed, setFailed] = useState(false);
  async function out() {
    const r = await api("/auth/logout", { method: "POST", csrf });
    // a 401 means the session is already gone: that is also signed out
    if (r.ok || r.problem.status === 401) hardNavigate(`/${lang}/sign-in`);
    else setFailed(true);
  }
  return (
    <>
      <button type="button" className="btn secondary" onClick={() => void out()}>
        {label}
      </button>
      {failed && <span role="alert" className="issue">{errors.network}</span>}
    </>
  );
}
