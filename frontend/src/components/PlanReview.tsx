"use client";
import { useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, hardNavigate, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
interface Assignment {
  lot_id: string;
  mandi_id: string;
  date: string;
  quantity_kg: string;
}
export interface Revision {
  revision_no: number;
  id: string | null;
  kind: string;
  created_at: string;
  assignments: Assignment[];
  changes: { lot_id: string; mandi_id: string; date: string; from_kg: string; to_kg: string }[];
  allocation_result: { totals: { scheduled_kg: string; expected_net_return_inr: string } };
}
interface Line {
  lot_id: string;
  reserved_kg: string;
  released_kg: string;
  remaining_kg: string;
}
export interface PlanState {
  status: string;
  current_revision_no: number;
  decision: { decision: string; revision_no: number; reason_code: string | null; decided_by: string; decided_at: string } | null;
  reservation: { state: string; lines: Line[]; release: { reason_code: string; released_at: string } | null };
}
export interface Context {
  revision_no: number;
  lines: { lot_id: string; assigned_kg: string; available_kg: string; can_reserve: boolean }[];
  context_warnings: { code: string; params: Text }[];
  required_acknowledgements: string[];
  blockers: string[];
  can_approve: boolean;
}
export interface Choice {
  lot_id: string;
  mandi_id: string;
  date: string;
}

const QTY = /^[0-9]+\.[0-9]{3}$/;
const fill = (s: string, v: Record<string, string>) => s.replace(/\{(\w+)\}/g, (w, k: string) => v[k] ?? w);

export function PlanReview({
  csrf,
  canWrite,
  canApprove,
  t,
  planWarningTexts,
  errors,
  planId,
  state,
  revisions,
  context,
  choices,
  lotLabels,
  mandiNames,
  userNames,
  categories,
}: {
  csrf: string;
  canWrite: boolean;
  canApprove: boolean;
  t: Dictionary["review"];
  planWarningTexts: Text;
  errors: Text;
  planId: string;
  state: PlanState;
  revisions: Revision[];
  context: Context;
  choices: Choice[];
  lotLabels: Text;
  mandiNames: Text;
  userNames: Text;
  categories: Text;
}) {
  const current = revisions[revisions.length - 1];
  const [shown, setShown] = useState(current.revision_no);
  const [qty, setQty] = useState<Record<string, string>>(() => {
    const m: Record<string, string> = {};
    for (const a of current.assignments) m[`${a.lot_id}|${a.mandi_id}|${a.date}`] = a.quantity_kg;
    return m;
  });
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [details, setDetails] = useState<string[]>([]);
  const [ticked, setTicked] = useState<string[]>([]);
  const [rejectReason, setRejectReason] = useState("PRICE_CONCERN");
  const [releaseReason, setReleaseReason] = useState("PLAN_CANCELLED");
  const [busy, setBusy] = useState(false);

  const issues: Text = t.issues;
  const lot = (id: string) => lotLabels[id] ?? id;
  const mandi = (id: string) => mandiNames[id] ?? id;
  const open = state.status === "PROPOSED";
  const rev = revisions.find((r) => r.revision_no === shown) ?? current;

  const ackText = (code: string): string => {
    const w = context.context_warnings.find((x) => x.code === code);
    if (w) {
      const p = { ...w.params };
      if (p.lot_id) p.lot = lot(p.lot_id);
      for (const k of ["from", "to"]) if (p[k]) p[k] = categories[p[k]] ?? p[k];
      return fill((t.contextWarnings as Text)[code] ?? code, p);
    }
    return planWarningTexts[code] ?? code;
  };

  function fail(p: ApiProblem, rawIssues: { code?: string; message?: string; path?: string }[] = []) {
    const text = issues[p.issueCode ?? ""] ?? problemText(errors, p);
    setMessage(flashFrom(text, p));
    setDetails(rawIssues.filter((i) => i.code !== "PLAN_INVALID").map((i) => `${i.path ?? ""} ${i.message ?? ""}`.trim()));
  }

  async function call<T>(path: string, body: unknown, key: string): Promise<T | null> {
    setBusy(true);
    setMessage(null);
    setDetails([]);
    const r = await api<T>(path, { method: "POST", csrf, body, idempotencyKey: key });
    setBusy(false);
    if (r.ok) return r.data;
    fail(r.problem, r.problem.issueList ?? []);
    return null;
  }

  async function saveEdit(e: React.FormEvent) {
    e.preventDefault();
    const lines = Object.entries(qty).filter(([, v]) => v !== "" && v !== "0.000");
    if (lines.some(([, v]) => !QTY.test(v))) return setMessage({ text: issues.PLAN_INVALID + " (0.000)", ref: "" });
    const body = {
      base_revision_no: current.revision_no,
      assignments: lines.map(([k, v]) => {
        const [lot_id, mandi_id, date] = k.split("|");
        return { lot_id, mandi_id, date, quantity_kg: v };
      }),
    };
    if (await call(`/plans/${planId}/revisions`, body, crypto.randomUUID())) hardNavigate(window.location.pathname);
  }

  async function approve() {
    if (!window.confirm(fill(t.confirmApprove, { n: String(current.revision_no) }))) return;
    const ok = await call(
      `/plans/${planId}/approve`,
      { revision_no: current.revision_no, acknowledged_warnings: ticked },
      crypto.randomUUID(),
    );
    if (ok) hardNavigate(window.location.pathname);
  }

  async function reject() {
    if (!window.confirm(t.confirmReject)) return;
    const ok = await call(`/plans/${planId}/reject`, { revision_no: current.revision_no, reason_code: rejectReason }, crypto.randomUUID());
    if (ok) hardNavigate(window.location.pathname);
  }

  async function release() {
    if (!window.confirm(t.confirmRelease)) return;
    const ok = await call(`/plans/${planId}/release`, { reason_code: releaseReason }, crypto.randomUUID());
    if (ok) hardNavigate(window.location.pathname);
  }

  const reasons = (m: unknown) => Object.entries(m as Text);
  const required = context.required_acknowledgements;
  const allTicked = required.every((c) => ticked.includes(c));

  return (
    <>
      <section className={state.status === "APPROVED" ? "card ready" : state.status === "PROPOSED" ? "card warn" : "card blocked"}>
        <h2>{t.title}</h2>
        <p>
          {t.state}: <strong>{(t.statusNames as Text)[state.status] ?? state.status}</strong>
          {state.reservation.state !== "NONE" && <> · {(t.reservationStates as Text)[state.reservation.state]}</>}
        </p>
        {state.decision && (
          <ul className="plain">
            <li>
              {t.decidedBy}: <strong>{userNames[state.decision.decided_by] ?? state.decision.decided_by}</strong>
            </li>
            <li>
              {t.decidedAt}: {state.decision.decided_at} · {fill(t.revision, { n: String(state.decision.revision_no) })}
            </li>
            {state.decision.reason_code && (
              <li>
                {t.reasonLabel}: {(t.rejectReasons as Text)[state.decision.reason_code] ?? state.decision.reason_code}
              </li>
            )}
          </ul>
        )}
        <Flash message={message} referenceLabel={errors.reference} />
        {details.length > 0 && (
          <ul className="muted">
            {details.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        )}
      </section>

      <h2>{t.revisions}</h2>
      <div className="row-actions">
        {revisions.map((r) => (
          <button key={r.revision_no} type="button" className={r.revision_no === shown ? "btn" : "btn secondary"} onClick={() => setShown(r.revision_no)}>
            {fill(t.revision, { n: String(r.revision_no) })}
            {r.revision_no === current.revision_no ? ` · ${t.current}` : ""}
          </button>
        ))}
      </div>
      <p className="muted">
        {(t.kinds as Text)[rev.kind]} · {rev.created_at} · {t.planned}: {rev.allocation_result.totals.scheduled_kg} · {t.net}: {rev.allocation_result.totals.expected_net_return_inr}
        {rev.revision_no < current.revision_no && ` · ${t.superseded}`}
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.lot}</th>
              <th>{t.mandi}</th>
              <th>{t.date}</th>
              <th>{t.quantity}</th>
            </tr>
          </thead>
          <tbody>
            {rev.assignments.map((a) => (
              <tr key={`${a.lot_id}|${a.mandi_id}|${a.date}`}>
                <td data-label={t.lot}>{lot(a.lot_id)}</td>
                <td data-label={t.mandi}>{mandi(a.mandi_id)}</td>
                <td data-label={t.date}>{a.date}</td>
                <td data-label={t.quantity}>{a.quantity_kg}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rev.changes.length > 0 ? (
        <>
          <h3>{fill(t.changesTitle, { n: String(rev.revision_no - 1 || 1) })}</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.lot}</th>
                  <th>{t.mandi}</th>
                  <th>{t.date}</th>
                  <th>{t.from}</th>
                  <th>{t.to}</th>
                </tr>
              </thead>
              <tbody>
                {rev.changes.map((c) => (
                  <tr key={`${c.lot_id}|${c.mandi_id}|${c.date}`}>
                    <td data-label={t.lot}>{lot(c.lot_id)}</td>
                    <td data-label={t.mandi}>{mandi(c.mandi_id)}</td>
                    <td data-label={t.date}>{c.date}</td>
                    <td data-label={t.from}>{c.from_kg}</td>
                    <td data-label={t.to}>{c.to_kg}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <p className="muted">{t.noChanges}</p>
      )}

      {open && canWrite && (
        <>
          <h2>{t.editTitle}</h2>
          <p className="muted">{t.editIntro}</p>
          <form onSubmit={saveEdit} className="form" noValidate>
            {choices.map((c) => {
              const k = `${c.lot_id}|${c.mandi_id}|${c.date}`;
              return (
                <label key={k}>
                  {lot(c.lot_id)} · {mandi(c.mandi_id)} · {c.date}
                  <input inputMode="decimal" autoComplete="off" placeholder="0.000" value={qty[k] ?? ""} onChange={(e) => setQty({ ...qty, [k]: e.target.value })} />
                </label>
              );
            })}
            <button type="submit" className="btn" disabled={busy}>
              {busy ? t.saving : t.save}
            </button>
          </form>
        </>
      )}
      {open && !canWrite && <p className="muted">{t.noEdit}</p>}

      {open && canApprove && (
        <>
          <h2>{t.approveTitle}</h2>
          <p className="muted">{t.approveIntro}</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.lotCol}</th>
                  <th>{t.assigned}</th>
                  <th>{t.available}</th>
                  <th>{t.canReserve}</th>
                </tr>
              </thead>
              <tbody>
                {context.lines.map((l) => (
                  <tr key={l.lot_id}>
                    <td data-label={t.lotCol}>{lot(l.lot_id)}</td>
                    <td data-label={t.assigned}>{l.assigned_kg}</td>
                    <td data-label={t.available}>{l.available_kg}</td>
                    <td data-label={t.canReserve}>{l.can_reserve ? t.yes : t.no}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {context.blockers.length > 0 && (
            <ul className="issue">
              {context.blockers.map((b) => (
                <li key={b}>{(t.blockers as Text)[b] ?? b}</li>
              ))}
            </ul>
          )}
          {required.length > 0 && (
            <fieldset>
              <legend>{t.ackTitle}</legend>
              {required.map((c) => (
                <label className="check" key={c}>
                  <input
                    type="checkbox"
                    checked={ticked.includes(c)}
                    onChange={(e) => setTicked(e.target.checked ? [...ticked, c] : ticked.filter((x) => x !== c))}
                  />{" "}
                  {ackText(c)}
                </label>
              ))}
            </fieldset>
          )}
          <p>
            <button type="button" className="btn" disabled={busy || !context.can_approve || !allTicked} onClick={() => void approve()}>
              {busy ? t.approving : fill(t.approveBtn, { n: String(current.revision_no) })}
            </button>
          </p>
          <h2>{t.rejectTitle}</h2>
          <div className="form">
            <label>
              {t.rejectReason}
              <select value={rejectReason} onChange={(e) => setRejectReason(e.target.value)}>
                {reasons(t.rejectReasons).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" className="btn danger" disabled={busy} onClick={() => void reject()}>
              {t.rejectBtn}
            </button>
          </div>
        </>
      )}
      {open && !canApprove && <p className="muted">{t.noRights}</p>}

      {state.reservation.state !== "NONE" && (
        <>
          <h2>{t.reservationTitle}</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t.lotCol}</th>
                  <th>{t.reserved}</th>
                  <th>{t.released}</th>
                  <th>{t.remaining}</th>
                </tr>
              </thead>
              <tbody>
                {state.reservation.lines.map((l) => (
                  <tr key={l.lot_id}>
                    <td data-label={t.lotCol}>{lot(l.lot_id)}</td>
                    <td data-label={t.reserved}>{l.reserved_kg}</td>
                    <td data-label={t.released}>{l.released_kg}</td>
                    <td data-label={t.remaining}>{l.remaining_kg}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {state.reservation.state === "ACTIVE" && canApprove && (
            <>
              <h3>{t.releaseTitle}</h3>
              <p className="muted">{t.releaseIntro}</p>
              <div className="form">
                <label>
                  {t.rejectReason}
                  <select value={releaseReason} onChange={(e) => setReleaseReason(e.target.value)}>
                    {reasons(t.releaseReasons).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <button type="button" className="btn danger" disabled={busy} onClick={() => void release()}>
                  {t.releaseBtn}
                </button>
              </div>
            </>
          )}
        </>
      )}
    </>
  );
}
