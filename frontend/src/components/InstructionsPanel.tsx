"use client";
import { useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
export interface Instruction {
  id: string;
  member_id: string;
  kind: string;
  version: number;
  status: string;
  issues: string[];
  language: string | null;
  template: { review_status: string } | null;
  text: string | null;
  delivery: { state: string; attempts: number };
}
interface Attempt {
  status: string;
  issues: string[];
  preview: { recipient: string; language: string; text: string; label: string } | null;
}

export function InstructionsPanel({
  csrf,
  canSend,
  t,
  langs,
  errors,
  planId,
  reservation,
  memberNames,
  initial,
}: {
  csrf: string;
  canSend: boolean;
  t: Dictionary["messages"];
  langs: Text;
  errors: Text;
  planId: string;
  reservation: string;
  memberNames: Text;
  initial: Instruction[];
}) {
  const [rows, setRows] = useState(initial);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [attempts, setAttempts] = useState<Record<string, Attempt>>({});
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [busy, setBusy] = useState(false);
  const issues: Text = t.issues;
  const errs: Text = t.errors;
  const delivery: Text = t.delivery;

  const fail = (p: ApiProblem) => setMessage(flashFrom(errs[p.issueCode ?? ""] ?? problemText(errors, p), p));

  async function prepare(kind: string) {
    setBusy(true);
    setMessage(null);
    const r = await api<Instruction[]>(`/plans/${planId}/instructions`, { method: "POST", csrf, body: { kind } });
    if (r.ok) {
      const all = await api<Instruction[]>(`/plans/${planId}/instructions`);
      setRows(all.ok ? all.data : r.data);
    } else fail(r.problem);
    setBusy(false);
  }

  async function preview(id: string) {
    setBusy(true);
    setMessage(null);
    const r = await api<Attempt>(`/instructions/${id}/send`, { method: "POST", csrf, body: { channel: "PREVIEW" } });
    if (r.ok) {
      setAttempts({ ...attempts, [id]: r.data });
      const all = await api<Instruction[]>(`/plans/${planId}/instructions`);
      if (all.ok) setRows(all.data);
    } else fail(r.problem);
    setBusy(false);
  }

  return (
    <>
      <h2>{t.title}</h2>
      <p className="muted">{t.intro}</p>
      <Flash message={message} referenceLabel={errors.reference} />
      {canSend && reservation === "ACTIVE" && (
        <p>
          <button type="button" className="btn" disabled={busy} onClick={() => void prepare("SALE_INSTRUCTION")}>
            {t.prepare}
          </button>
        </p>
      )}
      {canSend && reservation === "RELEASED" && (
        <>
          <p className="muted">{t.cancelIntro}</p>
          <p>
            <button type="button" className="btn" disabled={busy} onClick={() => void prepare("CANCELLATION")}>
              {t.prepareCancel}
            </button>
          </p>
        </>
      )}
      {!canSend && <p className="muted">{t.noRights}</p>}
      {rows.length === 0 && <p className="muted">{t.empty}</p>}
      {rows.map((r) => {
        const att = attempts[r.id];
        return (
          <section className="card" key={r.id}>
            <p>
              <strong>{memberNames[r.member_id] ?? r.member_id}</strong>{" "}
              <span className="muted">
                · {(t.kinds as Text)[r.kind]} · {r.language ? (langs[r.language] ?? r.language) : "—"} · v{r.version}
              </span>
            </p>
            <p>
              {t.status}: <strong>{(t.statuses as Text)[r.status]}</strong> · {t.deliveryCol}: <strong>{delivery[r.delivery.state]}</strong>
              {r.delivery.attempts > 0 && (
                <span className="muted">
                  {" "}
                  ({r.delivery.attempts} {t.attempts})
                </span>
              )}
            </p>
            {r.issues.length > 0 && (
              <ul className="issue">
                {r.issues.map((i) => (
                  <li key={i}>{issues[i] ?? i}</li>
                ))}
              </ul>
            )}
            {r.text && (
              <>
                <p className="muted">{r.template ? (t.reviewStatus as Text)[r.template.review_status] : ""}</p>
                <button type="button" className="btn secondary" onClick={() => setOpen({ ...open, [r.id]: !open[r.id] })}>
                  {open[r.id] ? t.hide : t.show}
                </button>{" "}
                {canSend && (
                  <button type="button" className="btn secondary" disabled={busy} onClick={() => void preview(r.id)}>
                    {busy ? t.previewing : t.preview}
                  </button>
                )}
                {open[r.id] && <pre className="message-text">{r.text}</pre>}
              </>
            )}
            {att && att.status === "PREVIEWED" && att.preview && (
              <div role="status">
                <p className="muted">
                  <strong>{att.preview.label}</strong> · {t.recipient}: <code>{att.preview.recipient}</code> · {langs[att.preview.language] ?? att.preview.language}
                </p>
                <pre className="message-text">{att.preview.text}</pre>
              </div>
            )}
            {att && att.status === "BLOCKED" && (
              <ul className="issue">
                {att.issues.map((i) => (
                  <li key={i}>{issues[i] ?? i}</li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </>
  );
}
