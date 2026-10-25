"use client";
import { useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText } from "@/lib/api-client";

export interface MandiRef {
  id: string;
  name: string;
}
interface PriceRow {
  mandi_id: string;
  variety_label: string;
  observed_on: string;
  min_inr_per_quintal: string | null;
  max_inr_per_quintal: string | null;
  modal_inr_per_quintal: string | null;
  arrivals_tonnes: string | null;
  date_inferred: boolean;
  flags: string[];
}

/** Filter and list daily mandi prices. Prices are shown exactly as stored (decimal strings), never re-rounded. */
export function PriceBrowser({
  t,
  errors,
  mandis,
  varieties,
}: {
  t: Dictionary["prices"];
  errors: Record<string, string>;
  mandis: MandiRef[];
  varieties: string[];
}) {
  const [f, setF] = useState({ mandi: "", variety: "", from: "", to: "" });
  const [rows, setRows] = useState<PriceRow[] | null>(null);
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [busy, setBusy] = useState(false);
  const names = new Map(mandis.map((m) => [m.id, m.name]));
  const flagNames: Record<string, string> = t.flagNames;

  async function show(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    const q = new URLSearchParams({ limit: "200" });
    if (f.mandi) q.set("mandi_id", f.mandi);
    if (f.variety) q.set("variety", f.variety);
    if (f.from) q.set("from", f.from);
    if (f.to) q.set("to", f.to);
    const r = await api<PriceRow[]>(`/prices?${q.toString()}`);
    setBusy(false);
    if (r.ok) setRows(r.data);
    else setMessage(flashFrom(problemText(errors, r.problem), r.problem));
  }

  const cell = (v: string | null) => v ?? <span className="muted">{t.missing}</span>;

  return (
    <>
      <h2>{t.filter}</h2>
      <form onSubmit={show} className="form" noValidate>
        <label>
          {t.mandi}
          <select value={f.mandi} onChange={(e) => setF({ ...f, mandi: e.target.value })}>
            <option value="">{t.allMandis}</option>
            {mandis.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t.variety}
          <select value={f.variety} onChange={(e) => setF({ ...f, variety: e.target.value })}>
            <option value="">{t.allVarieties}</option>
            {varieties.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t.from}
          <input type="date" value={f.from} onChange={(e) => setF({ ...f, from: e.target.value })} />
        </label>
        <label>
          {t.to}
          <input type="date" value={f.to} onChange={(e) => setF({ ...f, to: e.target.value })} />
        </label>
        <button type="submit" className="btn" disabled={busy}>
          {t.show}
        </button>
      </form>
      <Flash message={message} referenceLabel={errors.reference} />
      {rows && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t.date}</th>
                <th>{t.mandi}</th>
                <th>{t.variety}</th>
                <th>{t.modal}</th>
                <th>{t.min}</th>
                <th>{t.max}</th>
                <th>{t.arrivals}</th>
                <th>{t.flags}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.mandi_id}-${r.variety_label}-${r.observed_on}-${i}`}>
                  <td data-label={t.date}>
                    {r.observed_on}
                    {r.date_inferred && <span className="muted"> · {t.dateInferred}</span>}
                  </td>
                  <td data-label={t.mandi}>{names.get(r.mandi_id) ?? r.mandi_id}</td>
                  <td data-label={t.variety}>{r.variety_label}</td>
                  <td data-label={t.modal}>{cell(r.modal_inr_per_quintal)}</td>
                  <td data-label={t.min}>{cell(r.min_inr_per_quintal)}</td>
                  <td data-label={t.max}>{cell(r.max_inr_per_quintal)}</td>
                  <td data-label={t.arrivals}>{cell(r.arrivals_tonnes)}</td>
                  <td data-label={t.flags}>{r.flags.map((x) => flagNames[x] ?? x).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="muted">{t.none}</p>}
        </div>
      )}
    </>
  );
}
