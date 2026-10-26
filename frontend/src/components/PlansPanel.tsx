"use client";
import { useEffect, useRef, useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, hardNavigate, problemText } from "@/lib/api-client";

type Text = Record<string, string>;
export interface PlanSummary {
  id: string;
  created_at: string;
  variety: string;
  status: string;
  lots: number;
  farmers: number;
  allocated_kg: string;
  net_return_inr: string;
}
interface Range {
  low_inr_per_quintal: string;
  high_inr_per_quintal: string;
}
interface HorizonRow {
  h: number;
  target_date: string;
  point_inr_per_quintal: string;
  range80: Range | null;
  range95: Range | null;
  residual_pairs: number;
}
interface ForecastSet {
  origin_date: string;
  dataset: { validation_status: string };
  methods: { method: string; horizons: HorizonRow[] }[];
  candidate_mandis: { mandi_id: string; name: string; basis_inr_per_quintal: string | null; days_used: number; status: string }[];
}

const VARIETIES = ["1121", "Basmati 1509"] as const;
const TRANSPORT = /^(0|[1-9][0-9]{0,5})\.[0-9]{2}$/;
const INCREMENT = /^[0-9]+\.[0-9]{3}$/;
const fill = (s: string, v: Record<string, string>) => s.replace(/\{(\w+)\}/g, (w, k: string) => v[k] ?? w);
const newKey = () => crypto.randomUUID();

export function PlansPanel({
  lang,
  csrf,
  canWrite,
  t,
  datasetStatus,
  errors,
  initial,
}: {
  lang: string;
  csrf: string;
  canWrite: boolean;
  t: Dictionary["plans"];
  datasetStatus: Text;
  errors: Text;
  initial: PlanSummary[];
}) {
  const [variety, setVariety] = useState<string>(VARIETIES[0]);
  const [fc, setFc] = useState<ForecastSet | null | "none">(null);
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [horizons, setHorizons] = useState<number[]>([1, 7, 14]);
  const [increment, setIncrement] = useState("100.000");
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [busy, setBusy] = useState(false);
  const key = useRef(newKey());
  const statuses: Text = t.statuses;
  const methodNames: Text = t.methodNames;
  const issues: Text = t.issues;

  useEffect(() => {
    let live = true;
    void (async () => {
      const r = await api<ForecastSet>(`/forecasts?variety=${encodeURIComponent(variety)}`);
      if (!live) return;
      setPicked({});
      if (r.ok) setFc(r.data);
      else {
        setFc("none");
        setMessage(flashFrom(issues[r.problem.issueCode ?? ""] ?? problemText(errors, r.problem), r.problem));
      }
    })();
    return () => {
      live = false;
    };
  }, [variety, errors, issues]);

  function toggleMandi(id: string, on: boolean) {
    const next = { ...picked };
    if (on) next[id] = "0.00";
    else delete next[id];
    setPicked(next);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    const chosen = Object.entries(picked);
    if (chosen.length === 0) return setMessage({ text: t.selectMandi, ref: "" });
    if (chosen.some(([, v]) => !TRANSPORT.test(v))) return setMessage({ text: issues.TRANSPORT_INVALID, ref: "" });
    if (!INCREMENT.test(increment)) return setMessage({ text: issues.INCREMENT_INVALID, ref: "" });
    setBusy(true);
    const r = await api<{ id: string }>("/plans", {
      method: "POST",
      csrf,
      idempotencyKey: key.current,
      body: {
        variety,
        horizons_days: horizons,
        mandis: chosen.map(([mandi_id, transport_inr_per_quintal]) => ({ mandi_id, transport_inr_per_quintal })),
        increment_kg: increment,
      },
    });
    setBusy(false);
    if (r.ok) {
      key.current = newKey();
      hardNavigate(`/${lang}/plans/${r.data.id}`);
    } else setMessage(flashFrom(issues[r.problem.issueCode ?? ""] ?? problemText(errors, r.problem), r.problem));
  }

  const range = (r: Range | null, pairs: number) =>
    r ? `${r.low_inr_per_quintal} – ${r.high_inr_per_quintal}` : fill(t.noRange, { n: String(pairs) });

  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.created}</th>
              <th>{t.variety}</th>
              <th>{t.status}</th>
              <th>{t.farmers}</th>
              <th>{t.lots}</th>
              <th>{t.planned}</th>
              <th>{t.net}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {initial.map((p) => (
              <tr key={p.id}>
                <td data-label={t.created}>{p.created_at}</td>
                <td data-label={t.variety}>{p.variety}</td>
                <td data-label={t.status}>{statuses[p.status] ?? p.status}</td>
                <td data-label={t.farmers}>{p.farmers}</td>
                <td data-label={t.lots}>{p.lots}</td>
                <td data-label={t.planned}>{p.allocated_kg}</td>
                <td data-label={t.net}>{p.net_return_inr}</td>
                <td>
                  <a href={`/${lang}/plans/${p.id}`}>{t.open}</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {initial.length === 0 && <p className="muted">{t.empty}</p>}
      </div>

      {canWrite && (
        <>
          <h2>{t.new}</h2>
          <form onSubmit={submit} className="form" noValidate>
            <label>
              {t.variety}
              <select value={variety} onChange={(e) => setVariety(e.target.value)}>
                {VARIETIES.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            </label>

            <h3>{t.outlook}</h3>
            <p className="muted">
              {t.forecastLabel}. {t.notChecked}
            </p>
            {fc === null && <p className="muted">{t.loading}</p>}
            {fc === "none" && <p className="muted">{t.noOutlook}</p>}
            {fc !== null && fc !== "none" && (
              <>
                {fc.dataset.validation_status !== "VALIDATED" && (
                  <p role="note" className="issue">
                    {t.warnings.PRICE_DATASET_UNCHECKED} ({datasetStatus[fc.dataset.validation_status]})
                  </p>
                )}
                <p className="muted">{fill(t.origin, { date: fc.origin_date })}</p>
                {fc.methods.map((m) => (
                  <div className="table-wrap" key={m.method}>
                    <table>
                      <caption>{methodNames[m.method] ?? m.method}</caption>
                      <thead>
                        <tr>
                          <th />
                          <th>{t.point}</th>
                          <th>{t.range80}</th>
                          <th>{t.range95}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {m.horizons.map((h) => (
                          <tr key={h.h}>
                            <td data-label="">{fill(t.horizon, { h: String(h.h), date: h.target_date })}</td>
                            <td data-label={t.point}>{h.point_inr_per_quintal}</td>
                            <td data-label={t.range80}>{range(h.range80, h.residual_pairs)}</td>
                            <td data-label={t.range95}>{range(h.range95, h.residual_pairs)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}

                <fieldset>
                  <legend>{t.mandis}</legend>
                  <p className="muted">{t.mandiNote}</p>
                  {fc.candidate_mandis.map((c) => (
                    <div key={c.mandi_id}>
                      <label className="check">
                        <input
                          type="checkbox"
                          disabled={c.status !== "ELIGIBLE"}
                          checked={c.mandi_id in picked}
                          onChange={(e) => toggleMandi(c.mandi_id, e.target.checked)}
                        />{" "}
                        {c.name}{" "}
                        <span className="muted">
                          {c.status === "ELIGIBLE"
                            ? fill(t.basis, { basis: c.basis_inr_per_quintal ?? "", days: String(c.days_used) })
                            : fill(t.notEligible, { days: String(c.days_used) })}
                        </span>
                      </label>
                      {c.mandi_id in picked && (
                        <label>
                          {t.transport}
                          <input
                            inputMode="decimal"
                            autoComplete="off"
                            value={picked[c.mandi_id]}
                            onChange={(e) => setPicked({ ...picked, [c.mandi_id]: e.target.value })}
                          />
                        </label>
                      )}
                    </div>
                  ))}
                </fieldset>
              </>
            )}

            <fieldset>
              <legend>{t.horizons}</legend>
              {[1, 7, 14].map((h) => (
                <label className="check" key={h}>
                  <input
                    type="checkbox"
                    checked={horizons.includes(h)}
                    onChange={(e) => setHorizons(e.target.checked ? [...horizons, h].sort((a, b) => a - b) : horizons.filter((x) => x !== h))}
                  />{" "}
                  {h}
                </label>
              ))}
            </fieldset>
            <label>
              {t.increment}
              <input inputMode="decimal" autoComplete="off" value={increment} onChange={(e) => setIncrement(e.target.value)} />
              <span className="muted">{t.incrementHint}</span>
            </label>
            <Flash message={message} referenceLabel={errors.reference} />
            <button type="submit" className="btn" disabled={busy || fc === null || fc === "none" || horizons.length === 0}>
              {busy ? t.making : t.submit}
            </button>
          </form>
        </>
      )}
    </>
  );
}
