"use client";
import { useRef, useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
const format = (s: string, v: Record<string, string>) => s.replace(/\{(\w+)\}/g, (w, k: string) => v[k] ?? w);
interface Preview {
  recipient_count: number;
  unreviewed_count: number;
  ready: { instruction_id: string; member_id: string; display_name: string; language: string; review_status: string }[];
  blocked: { instruction_id: string; member_id: string; display_name: string; issues: string[] }[];
  already_reported_sent: string[];
}
interface Item {
  instruction_id: string;
  member_id: string;
  display_name: string;
  language: string | null;
  status: "ISSUED" | "BLOCKED";
  issues: string[];
  link: string | null;
  link_withheld: string[];
  state: "NOT_REPORTED" | "MANAGER_REPORTED_SENT" | "MANAGER_REPORTED_NOT_SENT";
}
interface Handover {
  id: string;
  created_at: string;
  recipient_count: number;
  items: Item[];
}
export type EarlierHandover = Handover;

/** The manager's own WhatsApp (handover/v1): Send to farmers -> yes or no -> one link per farmer -> "I sent it". The
 *  system sends nothing and cannot see delivery; what the manager reports is his statement, shown as such. */
export function HandoverPanel({
  csrf,
  canSend,
  canLink,
  t,
  langs,
  issueTexts,
  errors,
  planId,
  reservation,
  earlier: initialEarlier,
}: {
  csrf: string;
  canSend: boolean;
  canLink: boolean;
  t: Dictionary["handover"];
  langs: Text;
  issueTexts: Text;
  errors: Text;
  planId: string;
  reservation: string;
  earlier: EarlierHandover[];
}) {
  const kind = reservation === "RELEASED" ? "CANCELLATION" : "SALE_INSTRUCTION";
  const [phase, setPhase] = useState<"idle" | "asking" | "links">("idle");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [handover, setHandover] = useState<Handover | null>(null);
  const [earlier, setEarlier] = useState<Handover[]>(initialEarlier);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [declined, setDeclined] = useState(false);
  const pressKey = useRef<string>("");
  const errs: Text = t.errors;

  const fail = (p: ApiProblem) => setMessage(flashFrom(errs[p.issueCode ?? ""] ?? problemText(errors, p), p));

  async function loadEarlier() {
    const r = await api<Handover[]>(`/plans/${planId}/handovers`);
    if (r.ok) setEarlier(r.data.filter((h) => h.id !== handover?.id));
  }

  async function start() {
    setBusy(true);
    setMessage(null);
    setDeclined(false);
    const prepared = await api(`/plans/${planId}/instructions`, { method: "POST", csrf, body: { kind } });
    if (!prepared.ok) {
      fail(prepared.problem);
      setBusy(false);
      return;
    }
    const r = await api<Preview>(`/plans/${planId}/handover-preview?kind=${kind}`);
    if (r.ok) {
      setPreview(r.data);
      setAck(false);
      pressKey.current = crypto.randomUUID();
      setPhase("asking");
    } else fail(r.problem);
    setBusy(false);
  }

  async function yes() {
    if (!preview) return;
    setBusy(true);
    setMessage(null);
    const r = await api<Handover>(`/plans/${planId}/handovers`, {
      method: "POST",
      csrf,
      idempotencyKey: pressKey.current,
      body: { kind, recipient_count: preview.recipient_count, acknowledge_unreviewed: ack },
    });
    if (r.ok) {
      setHandover(r.data);
      setPhase("links");
      void loadEarlier();
    } else fail(r.problem);
    setBusy(false);
  }

  function no() {
    setPhase("idle");
    setPreview(null);
    setDeclined(true);
  }

  async function mark(item: Item, outcome: "SENT" | "NOT_SENT") {
    if (!handover) return;
    setMessage(null);
    const r = await api<Item>(`/handovers/${handover.id}/items/${item.instruction_id}/outcome`, {
      method: "POST",
      csrf,
      body: { outcome },
    });
    if (!r.ok) return fail(r.problem);
    setHandover({
      ...handover,
      items: handover.items.map((i) =>
        i.instruction_id === item.instruction_id ? { ...i, state: r.data.state, link: outcome === "SENT" ? null : i.link } : i,
      ),
    });
  }

  async function show(id: string) {
    setBusy(true);
    const r = await api<Handover>(`/handovers/${id}`);
    if (r.ok) {
      setHandover(r.data);
      setPhase("links");
    } else fail(r.problem);
    setBusy(false);
  }

  if (!canSend) return null;
  const issueText = (code: string) => issueTexts[code] ?? code;

  return (
    <section className="card" aria-labelledby="handover-title">
      <h2 id="handover-title">{t.title}</h2>
      <p className="muted">{t.intro}</p>
      <Flash message={message} referenceLabel={errors.reference} />
      {declined && <p className="muted">{t.declined}</p>}
      {!canLink && <p className="muted">{t.noRights}</p>}

      {canLink && phase === "idle" && (
        <p>
          <button type="button" className="btn" disabled={busy} onClick={() => void start()}>
            {busy ? t.preparing : kind === "CANCELLATION" ? t.sendCancel : t.send}
          </button>
        </p>
      )}

      {canLink && phase === "asking" && preview && (
        <div role="alertdialog" aria-labelledby="handover-ask">
          {preview.recipient_count === 0 ? (
            <p id="handover-ask">
              <strong>{t.nothing}</strong>
            </p>
          ) : (
            <p id="handover-ask">
              <strong>{format(t.askTitle, { count: String(preview.recipient_count) })}</strong>
            </p>
          )}
          <p className="muted">{t.askBody}</p>
          <ul className="plain">
            {preview.ready.map((x) => (
              <li key={x.instruction_id}>
                {x.display_name} <span className="muted">· {langs[x.language] ?? x.language}</span>
              </li>
            ))}
          </ul>
          {preview.already_reported_sent.length > 0 && (
            <p className="muted">{format(t.alreadySent, { count: String(preview.already_reported_sent.length) })}</p>
          )}
          {preview.blocked.length > 0 && (
            <>
              <p>{t.cannotSend}</p>
              <ul className="issue">
                {preview.blocked.map((x) => (
                  <li key={x.instruction_id}>
                    {x.display_name}: {x.issues.map(issueText).join(" ")}
                  </li>
                ))}
              </ul>
            </>
          )}
          {preview.unreviewed_count > 0 && (
            <label>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />{" "}
              {format(t.unreviewed, { count: String(preview.unreviewed_count) })} {t.ack}
            </label>
          )}
          <p>
            <button
              type="button"
              className="btn"
              disabled={busy || preview.recipient_count === 0 || (preview.unreviewed_count > 0 && !ack)}
              onClick={() => void yes()}
            >
              {t.yes}
            </button>{" "}
            <button type="button" className="btn secondary" disabled={busy} onClick={no}>
              {t.no}
            </button>
          </p>
        </div>
      )}

      {canLink && phase === "links" && handover && (
        <div>
          <h3>{t.linksTitle}</h3>
          <p className="muted">{t.label}</p>
          {handover.items.map((i) => (
            <div className="card" key={i.instruction_id}>
              <p>
                <strong>{i.display_name}</strong>{" "}
                <span className="muted">
                  · {i.language ? (langs[i.language] ?? i.language) : "—"} ·{" "}
                  {i.status === "BLOCKED" ? "" : (t.states as Text)[i.state]}
                </span>
              </p>
              {i.status === "BLOCKED" && <ul className="issue">{i.issues.map((c) => <li key={c}>{issueText(c)}</li>)}</ul>}
              {i.status === "ISSUED" && i.link && (
                <p>
                  <a className="btn" href={i.link} target="_blank" rel="noopener noreferrer">
                    {t.open}
                  </a>{" "}
                  <button type="button" className="btn secondary" onClick={() => void mark(i, "SENT")}>
                    {t.markSent}
                  </button>{" "}
                  {i.state === "MANAGER_REPORTED_NOT_SENT" ? null : (
                    <button type="button" className="btn secondary" onClick={() => void mark(i, "NOT_SENT")}>
                      {t.markNotSent}
                    </button>
                  )}
                </p>
              )}
              {i.status === "ISSUED" && !i.link && i.link_withheld.length > 0 && (
                <p className="muted">
                  {t.noLink} {i.link_withheld.map(issueText).join(" ")}
                </p>
              )}
              {i.status === "ISSUED" && i.state === "MANAGER_REPORTED_SENT" && (
                <button type="button" className="btn secondary" onClick={() => void mark(i, "NOT_SENT")}>
                  {t.markNotSent}
                </button>
              )}
            </div>
          ))}
          <p>
            <button type="button" className="btn secondary" onClick={() => setPhase("idle")}>
              {t.no}
            </button>
          </p>
        </div>
      )}

      {canLink && earlier.length > 0 && phase !== "links" && (
        <>
          <h3>{t.earlier}</h3>
          <ul className="plain">
            {earlier.map((h) => (
              <li key={h.id}>
                {format(t.earlierRow, { when: h.created_at, count: String(h.recipient_count) })} ·{" "}
                {format(t.reportedCount, { sent: String(h.items.filter((i) => i.state === "MANAGER_REPORTED_SENT").length) })}{" "}
                <button type="button" className="btn secondary" disabled={busy} onClick={() => void show(h.id)}>
                  {t.showLinks}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
