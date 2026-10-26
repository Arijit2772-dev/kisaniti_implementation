"use client";
import { useRef, useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
export interface Sale {
  id: string;
  lot_id: string;
  plan_ref: { plan_id: string; mandi_id: string; date: string } | null;
  mandi_id: string;
  sale_date: string;
  quantity_kg: string;
  price_inr_per_quintal: string;
  amount_inr: string;
  j_form_no: string;
  consumed_kg: string;
  released_for_sale: { plan_id: string; quantity_kg: string }[];
  voided: { reason_code: string } | null;
}
export interface PlanLine {
  plan_id: string;
  lot_id: string;
  mandi_id: string;
  date: string;
  label: string;
}

const fill = (s: string, v: Record<string, string>) => s.replace(/\{(\w+)\}/g, (w, k: string) => v[k] ?? w);
const newKey = () => crypto.randomUUID();

export function SalesPanel({
  lang,
  csrf,
  canWrite,
  canVoid,
  t,
  errors,
  initial,
  lotLabels,
  mandis,
  planLines,
}: {
  lang: string;
  csrf: string;
  canWrite: boolean;
  canVoid: boolean;
  t: Dictionary["sales"];
  errors: Text;
  initial: Sale[];
  lotLabels: Text;
  mandis: { id: string; name: string }[];
  planLines: PlanLine[];
}) {
  const [sales, setSales] = useState(initial);
  const [f, setF] = useState({ lot: "", line: "", mandi: "", date: "", qty: "", price: "", jform: "" });
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [holds, setHolds] = useState<{ plan: string; kg: string }[] | null>(null);
  const [shortfall, setShortfall] = useState("");
  const [releases, setReleases] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const keyRef = useRef(newKey());
  const issues: Text = t.issues;
  const mandiName = (id: string) => mandis.find((m) => m.id === id)?.name ?? id;
  const lines = planLines.filter((l) => l.lot_id === f.lot);

  function fail(p: ApiProblem) {
    if (p.issueCode === "RECONCILIATION_REQUIRED") {
      const list = (p.issueList ?? []).slice(1).map((i) => ({ plan: String(i.path ?? ""), kg: String(i.message ?? "") }));
      setHolds(list);
      setShortfall(/([0-9]+\.[0-9]{3}) kg more/.exec(p.issueList?.[0]?.message ?? "")?.[1] ?? "");
      setReleases({});
      return;
    }
    setMessage(flashFrom(issues[p.issueCode ?? ""] ?? problemText(errors, p), p));
  }

  async function reload() {
    const r = await api<Sale[]>("/sales");
    if (r.ok) setSales(r.data);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    const chosen = holds ? Object.entries(releases).filter(([, v]) => v && v !== "0.000") : [];
    const line = planLines.find((l) => `${l.plan_id}|${l.mandi_id}|${l.date}` === f.line && l.lot_id === f.lot);
    setBusy(true);
    const r = await api("/sales", {
      method: "POST",
      csrf,
      idempotencyKey: keyRef.current,
      body: {
        lot_id: f.lot,
        plan_ref: line ? { plan_id: line.plan_id, mandi_id: line.mandi_id, date: line.date } : null,
        mandi_id: f.mandi,
        sale_date: f.date,
        quantity_kg: f.qty,
        price_inr_per_quintal: f.price,
        j_form_no: f.jform,
        ...(chosen.length ? { release_reservations: chosen.map(([plan_id, quantity_kg]) => ({ plan_id, quantity_kg })) } : {}),
      },
    });
    setBusy(false);
    if (r.ok) {
      keyRef.current = newKey();
      setHolds(null);
      setF({ ...f, qty: "", price: "", jform: "" });
      await reload();
    } else {
      keyRef.current = newKey();
      fail(r.problem);
    }
  }

  async function voidSale(id: string) {
    if (!window.confirm(t.confirmVoid)) return;
    const reason = window.prompt(Object.entries(t.voidReasons as Text).map(([k, v]) => `${k} = ${v}`).join("\n"), "ENTRY_ERROR");
    if (!reason) return;
    const r = await api(`/sales/${id}/void`, { method: "POST", csrf, body: { reason_code: reason } });
    if (r.ok) await reload();
    else fail(r.problem);
  }

  return (
    <>
      <Flash message={message} referenceLabel={errors.reference} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.when}</th>
              <th>{t.lot}</th>
              <th>{t.mandi}</th>
              <th>{t.quantity}</th>
              <th>{t.price}</th>
              <th>{t.amount}</th>
              <th>{t.jform}</th>
              <th>{t.plan}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sales.map((s) => (
              <tr key={s.id}>
                <td data-label={t.when}>{s.sale_date}</td>
                <td data-label={t.lot}>{lotLabels[s.lot_id] ?? s.lot_id}</td>
                <td data-label={t.mandi}>{mandiName(s.mandi_id)}</td>
                <td data-label={t.quantity}>{s.quantity_kg}</td>
                <td data-label={t.price}>{s.price_inr_per_quintal}</td>
                <td data-label={t.amount}>{s.amount_inr}</td>
                <td data-label={t.jform}>{s.j_form_no}</td>
                <td data-label={t.plan}>{s.plan_ref ? <a href={`/${lang}/plans/${s.plan_ref.plan_id}`}>{s.plan_ref.date}</a> : <span className="muted">{t.offPlan}</span>}</td>
                <td className="row-actions">
                  {s.voided ? (
                    <span className="muted">{t.voided}: {(t.voidReasons as Text)[s.voided.reason_code] ?? s.voided.reason_code}</span>
                  ) : (
                    canVoid && (
                      <button type="button" className="btn danger" onClick={() => void voidSale(s.id)}>
                        {t.void}
                      </button>
                    )
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {sales.length === 0 && <p className="muted">{t.empty}</p>}
      </div>
      {canWrite ? (
        <form onSubmit={submit} className="form" noValidate>
          <label>
            {t.lot}
            <select required value={f.lot} onChange={(e) => setF({ ...f, lot: e.target.value, line: "" })}>
              <option value="" />
              {Object.entries(lotLabels).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t.planLine}
            <select value={f.line} onChange={(e) => setF({ ...f, line: e.target.value })}>
              <option value="">{t.noPlan}</option>
              {lines.map((l) => (
                <option key={`${l.plan_id}|${l.mandi_id}|${l.date}`} value={`${l.plan_id}|${l.mandi_id}|${l.date}`}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t.mandi}
            <select required value={f.mandi} onChange={(e) => setF({ ...f, mandi: e.target.value })}>
              <option value="" />
              {mandis.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t.date}
            <input type="date" required value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} />
          </label>
          <label>
            {t.quantity}
            <input required inputMode="decimal" autoComplete="off" placeholder="0.000" value={f.qty} onChange={(e) => setF({ ...f, qty: e.target.value })} />
          </label>
          <label>
            {t.price}
            <input required inputMode="decimal" autoComplete="off" placeholder="0.00" value={f.price} onChange={(e) => setF({ ...f, price: e.target.value })} />
          </label>
          <label>
            {t.jform}
            <input required autoComplete="off" value={f.jform} onChange={(e) => setF({ ...f, jform: e.target.value })} />
          </label>
          {holds && (
            <fieldset>
              <legend>{t.reconcileTitle}</legend>
              <p>{fill(t.reconcileIntro, { kg: shortfall || "?" })}</p>
              {holds.map((h) => (
                <label key={h.plan}>
                  {fill(t.holds, { plan: h.plan.slice(0, 12) + "…", kg: h.kg })}
                  <input inputMode="decimal" autoComplete="off" placeholder={`${t.release} 0.000`} value={releases[h.plan] ?? ""} onChange={(e) => setReleases({ ...releases, [h.plan]: e.target.value })} />
                </label>
              ))}
            </fieldset>
          )}
          <button type="submit" className="btn" disabled={busy || !f.lot || !f.mandi || !f.date || !f.qty || !f.price || !f.jform}>
            {busy ? t.recording : holds ? t.resubmit : t.submit}
          </button>
        </form>
      ) : (
        <p className="muted">{t.noWrite}</p>
      )}
    </>
  );
}

