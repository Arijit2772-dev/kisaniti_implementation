"use client";
import { useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
export interface ContactRow {
  member_id: string;
  display_name: string;
  language: string;
  active: boolean;
  contact: { phone: string; phone_masked: boolean; version: number } | null;
  consents: Record<string, string>;
}

const NOTICE = "NOTICE-DEMO-0.1";
const KINDS = ["WHATSAPP", "ADVICE", "RESEARCH"] as const;

export function ContactsPanel({
  csrf,
  canWrite,
  t,
  langs,
  errors,
  initial,
}: {
  csrf: string;
  canWrite: boolean;
  t: Dictionary["contacts"];
  langs: Text;
  errors: Text;
  initial: ContactRow[];
}) {
  const [rows, setRows] = useState(initial);
  const [phones, setPhones] = useState<Record<string, string>>({});
  const [method, setMethod] = useState("STAFF_ASSISTED_ORAL");
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [saved, setSaved] = useState(false);
  const issues: Text = t.issues;
  const states: Text = t.states;
  const kinds: Text = t.kinds;

  const fail = (p: ApiProblem) => setMessage(flashFrom(issues[p.issueCode ?? ""] ?? problemText(errors, p), p));

  async function reload() {
    const r = await api<ContactRow[]>("/contacts");
    if (r.ok) setRows(r.data);
    else fail(r.problem);
  }

  async function savePhone(id: string) {
    setMessage(null);
    setSaved(false);
    const r = await api(`/members/${id}/contact`, { method: "POST", csrf, body: { phone: phones[id] ?? "" } });
    if (r.ok) {
      setPhones({ ...phones, [id]: "" });
      setSaved(true);
      await reload();
    } else fail(r.problem);
  }

  async function record(id: string, kind: string, current: string) {
    setMessage(null);
    setSaved(false);
    const action = current === "GRANTED" ? "WITHDRAWN" : "GRANTED";
    const r = await api(`/members/${id}/consents`, { method: "POST", csrf, body: { kind, action, notice_version: NOTICE, method } });
    if (r.ok) {
      setSaved(true);
      await reload();
    } else fail(r.problem);
  }

  return (
    <>
      <Flash message={message} referenceLabel={errors.reference} />
      {saved && <p role="status">{t.saved}</p>}
      {canWrite && (
        <label>
          {t.method}
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            {Object.entries(t.methods as Text).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          <span className="muted">
            {t.notice}: {NOTICE}
          </span>
        </label>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.member}</th>
              <th>{t.language}</th>
              <th>{t.phone}</th>
              {KINDS.map((k) => (
                <th key={k}>{kinds[k]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.member_id}>
                <td data-label={t.member}>
                  {r.display_name}
                  {!r.active && <span className="muted"> · {t.inactive}</span>}
                </td>
                <td data-label={t.language}>{langs[r.language] ?? r.language}</td>
                <td data-label={t.phone}>
                  {r.contact ? <code>{r.contact.phone}</code> : <span className="muted">{t.noPhone}</span>}
                  {canWrite && (
                    <span className="inline-move">
                      <input
                        aria-label={t.newPhone}
                        inputMode="tel"
                        autoComplete="off"
                        placeholder="90000 00000"
                        value={phones[r.member_id] ?? ""}
                        onChange={(e) => setPhones({ ...phones, [r.member_id]: e.target.value })}
                      />
                      <button type="button" className="btn secondary" disabled={!(phones[r.member_id] ?? "").trim()} onClick={() => void savePhone(r.member_id)}>
                        {t.setPhone}
                      </button>
                    </span>
                  )}
                </td>
                {KINDS.map((k) => (
                  <td key={k} data-label={kinds[k]}>
                    <strong>{states[r.consents[k]] ?? r.consents[k]}</strong>
                    {canWrite && (
                      <span className="inline-move">
                        <button type="button" className="btn secondary" onClick={() => void record(r.member_id, k, r.consents[k])}>
                          {r.consents[k] === "GRANTED" ? t.withdraw : t.grant}
                        </button>
                      </span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="muted">—</p>}
      </div>
      {rows.some((r) => r.contact?.phone_masked) && <p className="muted">{t.maskedNote}</p>}
      {!canWrite && <p className="muted">{t.noWrite}</p>}
    </>
  );
}
