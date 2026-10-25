"use client";
import { useCallback, useState } from "react";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
export interface UserRow {
  id: string;
  username: string;
  display_name: string;
  role: string;
  extra_capabilities: string[];
  active: boolean;
  must_change_password: boolean;
}
interface Created {
  user: UserRow;
  temporary_password: string;
}

const CREATABLE = ["FPO_ADMIN", "STAFF", "VIEWER"] as const;

export function UsersPanel({ csrf, selfId, t, errors, roles, initialUsers }: { csrf: string; selfId: string; t: Text; errors: Text; roles: Text; initialUsers: UserRow[] }) {
  const [users, setUsers] = useState<UserRow[]>(initialUsers);
  const [message, setMessage] = useState<{ text: string; ref: string } | null>(null);
  const [created, setCreated] = useState<Created | null>(null);
  const [copied, setCopied] = useState(false);
  const [form, setForm] = useState({ username: "", display_name: "", role: "STAFF", messages: false, phones: false });
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (p: ApiProblem, special?: string) => setMessage({ text: special ?? problemText(errors, p), ref: p.requestId }),
    [errors],
  );

  const load = useCallback(async () => {
    const r = await api<UserRow[]>("/users");
    if (r.ok) setUsers(r.data);
    else fail(r.problem);
  }, [fail]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    setCreated(null);
    const extra = form.role === "STAFF" ? [form.messages && "messages.send", form.phones && "contacts.view_phone"].filter(Boolean) : [];
    const r = await api<Created>("/users", {
      method: "POST",
      csrf,
      body: { username: form.username, display_name: form.display_name, role: form.role, extra_capabilities: extra },
    });
    setBusy(false);
    if (r.ok) {
      setCreated(r.data);
      setCopied(false);
      setForm({ username: "", display_name: "", role: "STAFF", messages: false, phones: false });
      await load();
    } else fail(r.problem, r.problem.code === "CONFLICT" ? t.taken : undefined);
  }

  async function reset(u: UserRow) {
    if (!window.confirm(t.confirmReset.replace("{name}", u.display_name))) return;
    setMessage(null);
    const r = await api<Created>(`/users/${u.id}/reset-password`, { method: "POST", csrf });
    if (r.ok) {
      setCreated(r.data);
      setCopied(false);
      await load();
    } else fail(r.problem);
  }

  async function deactivate(u: UserRow) {
    if (!window.confirm(t.confirmDeactivate.replace("{name}", u.display_name))) return;
    setMessage(null);
    const r = await api(`/users/${u.id}/deactivate`, { method: "POST", csrf });
    if (r.ok) await load();
    else fail(r.problem);
  }

  return (
    <>
      {message && (
        <p role="alert" className="issue">
          {message.text}
          {message.ref && (
            <span className="muted">
              {" "}
              ({errors.reference}: {message.ref})
            </span>
          )}
        </p>
      )}
      {created && (
        <section className="card ready" role="status">
          <p>
            <strong>{created.user.display_name}</strong> ({created.user.username}). {t.created}
          </p>
          <p>
            {t.temporary}: <code className="temp">{created.temporary_password}</code>{" "}
            <button
              type="button"
              className="btn secondary"
              onClick={() => void navigator.clipboard.writeText(created.temporary_password).then(() => setCopied(true))}
            >
              {copied ? "✓" : "⧉"}
            </button>
          </p>
        </section>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.username}</th>
              <th>{t.name}</th>
              <th>{t.role}</th>
              <th>{t.status}</th>
              <th>{t.actions}</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td data-label={t.username}>{u.username}</td>
                <td data-label={t.name}>{u.display_name}</td>
                <td data-label={t.role}>{roles[u.role] ?? u.role}</td>
                <td data-label={t.status}>
                  {u.active ? t.active : t.inactive}
                  {u.must_change_password && <span className="muted"> · {t.mustChange}</span>}
                </td>
                <td className="row-actions" data-label={t.actions}>
                  <button type="button" className="btn secondary" onClick={() => void reset(u)}>
                    {t.reset}
                  </button>
                  {u.id !== selfId && u.active && (
                    <button type="button" className="btn danger" onClick={() => void deactivate(u)}>
                      {t.deactivate}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.length === 0 && <p className="muted">{t.empty}</p>}
      </div>

      <h2>{t.add}</h2>
      <form onSubmit={create} className="form" noValidate>
        <label>
          {t.username}
          <input required autoCapitalize="none" spellCheck={false} pattern="[a-z0-9][a-z0-9._\-]{2,63}" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value.toLowerCase() })} />
          <span className="muted">{t.usernameHint}</span>
        </label>
        <label>
          {t.displayName}
          <input required maxLength={120} value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        </label>
        <label>
          {t.role}
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
            {CREATABLE.map((r) => (
              <option key={r} value={r}>
                {roles[r]}
              </option>
            ))}
          </select>
        </label>
        {form.role === "STAFF" && (
          <fieldset>
            <legend>{t.extra}</legend>
            <label className="check">
              <input type="checkbox" checked={form.messages} onChange={(e) => setForm({ ...form, messages: e.target.checked })} /> {t.extraMessages}
            </label>
            <label className="check">
              <input type="checkbox" checked={form.phones} onChange={(e) => setForm({ ...form, phones: e.target.checked })} /> {t.extraPhones}
            </label>
          </fieldset>
        )}
        <button type="submit" className="btn" disabled={busy || !form.username || !form.display_name}>
          {busy ? t.creating : t.submit}
        </button>
      </form>
    </>
  );
}
