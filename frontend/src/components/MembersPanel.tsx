"use client";
import { useCallback, useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText } from "@/lib/api-client";

type Text = Record<string, string>;
export interface MemberRow {
  id: string;
  display_name: string;
  village: string | null;
  preferred_language: "pa" | "hi" | "en";
  active: boolean;
  origin: string;
}

export function MembersPanel({
  csrf,
  canWrite,
  t,
  errors,
  initial,
}: {
  csrf: string;
  canWrite: boolean;
  t: Dictionary["members"];
  errors: Text;
  initial: MemberRow[];
}) {
  const langs: Text = t.langs;
  const [rows, setRows] = useState<MemberRow[]>(initial);
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [form, setForm] = useState({ display_name: "", village: "", preferred_language: "pa" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await api<MemberRow[]>("/members");
    if (r.ok) setRows(r.data);
    else setMessage(flashFrom(problemText(errors, r.problem), r.problem));
  }, [errors]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    const r = await api("/members", {
      method: "POST",
      csrf,
      body: {
        display_name: form.display_name.trim(),
        village: form.village.trim() || null,
        preferred_language: form.preferred_language,
      },
    });
    setBusy(false);
    if (r.ok) {
      setForm({ display_name: "", village: "", preferred_language: form.preferred_language });
      await load();
    } else setMessage(flashFrom(problemText(errors, r.problem), r.problem));
  }

  async function toggle(m: MemberRow) {
    setMessage(null);
    const r = await api(`/members/${m.id}`, { method: "PATCH", csrf, body: { active: !m.active } });
    if (r.ok) await load();
    else setMessage(flashFrom(problemText(errors, r.problem), r.problem));
  }

  return (
    <>
      <Flash message={message} referenceLabel={errors.reference} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.name}</th>
              <th>{t.village}</th>
              <th>{t.language}</th>
              <th>{t.status}</th>
              {canWrite && <th />}
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.id}>
                <td data-label={t.name}>{m.display_name}</td>
                <td data-label={t.village}>{m.village ?? "—"}</td>
                <td data-label={t.language}>{langs[m.preferred_language] ?? m.preferred_language}</td>
                <td data-label={t.status}>{m.active ? t.active : t.inactive}</td>
                {canWrite && (
                  <td className="row-actions">
                    <button type="button" className="btn secondary" onClick={() => void toggle(m)}>
                      {m.active ? t.deactivate : t.activate}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="muted">{t.empty}</p>}
      </div>
      {canWrite ? (
        <>
          <h2>{t.add}</h2>
          <form onSubmit={add} className="form" noValidate>
            <label>
              {t.name}
              <input required maxLength={120} value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
            </label>
            <label>
              {t.village}
              <input maxLength={100} value={form.village} onChange={(e) => setForm({ ...form, village: e.target.value })} />
            </label>
            <label>
              {t.language}
              <select value={form.preferred_language} onChange={(e) => setForm({ ...form, preferred_language: e.target.value })}>
                {Object.entries(langs).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className="btn" disabled={busy || !form.display_name.trim()}>
              {busy ? t.adding : t.submit}
            </button>
          </form>
        </>
      ) : (
        <p className="muted">{t.noWrite}</p>
      )}
    </>
  );
}
