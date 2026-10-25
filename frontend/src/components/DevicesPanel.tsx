"use client";
import { useCallback, useEffect, useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
export interface DeviceRow {
  device_id: string;
  source_origin: string;
  active: boolean;
  current_assignment: { placement: string; crop: string; assigned_at: string; sample_interval_s: number } | null;
  last_reading_at: string | null;
}
interface Assessment {
  assessed_at: string;
  category: "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN";
  unknown_reasons: string[];
  adjustments: string[];
  rh_median_pct: number | null;
  temperature_median_c: number | null;
  insect_flag: string | null;
  samples_used: number;
  samples_expected: number;
  label: string;
  provenance: { origin: string };
}
interface Reading {
  measured_at: string;
  measured_at_trusted: boolean;
  humidity_pct: number | null;
  temperature_c: number | null;
  origin: string;
}
interface Detail {
  assessment: Assessment | null;
  readings: Reading[];
  fetchedAt: number;
}

const HOUR = 3600_000;

/** Humidity over the last 24 hours as a plain line chart; the 65 % and 70 % limits of STORAGE-DEMO-0.1 are drawn. */
function Trend({ readings, now, label, limits }: { readings: Reading[]; now: number; label: string; limits: string }) {
  const pts = readings
    .filter((r) => r.measured_at_trusted && r.humidity_pct !== null)
    .map((r) => ({ t: Date.parse(r.measured_at), v: r.humidity_pct as number }))
    .filter((p) => p.t >= now - 24 * HOUR && p.t <= now + HOUR)
    .sort((a, b) => a.t - b.t);
  if (pts.length < 2) return null;
  const W = 400;
  const H = 140;
  const lo = Math.min(50, ...pts.map((p) => p.v)) - 2;
  const hi = Math.max(75, ...pts.map((p) => p.v)) + 2;
  const x = (t: number) => ((t - (now - 24 * HOUR)) / (24 * HOUR)) * W;
  const y = (v: number) => H - ((v - lo) / (hi - lo)) * H;
  return (
    <figure>
      <svg className="trend" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
        {[65, 70].map((v) => (
          <line key={v} className="limit" x1={0} x2={W} y1={y(v)} y2={y(v)} />
        ))}
        <polyline className="line" points={pts.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")} />
      </svg>
      <figcaption className="muted">
        {label} · {limits}
      </figcaption>
    </figure>
  );
}

export function DevicesPanel({
  csrf,
  canWrite,
  canManage,
  t,
  errors,
  initial,
}: {
  csrf: string;
  canWrite: boolean;
  canManage: boolean;
  t: Dictionary["devices"];
  errors: Text;
  initial: DeviceRow[];
}) {
  const [devices, setDevices] = useState(initial);
  const [detail, setDetail] = useState<Record<string, Detail>>({});
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [reg, setReg] = useState({ device_id: "", source_origin: "SIMULATOR" });
  const [asg, setAsg] = useState<Record<string, { placement: string; crop: string; interval: string }>>({});
  const origins: Text = t.origins;
  const placements: Text = t.placements;
  const crops: Text = t.crops;
  const categories: Text = t.categories;
  const reasons: Text = t.reasons;
  const insects: Text = t.insects;
  const adjust: Text = t.adjust;

  const fail = useCallback((p: ApiProblem) => setMessage(flashFrom(problemText(errors, p), p)), [errors]);

  const loadDetail = useCallback(
    async (id: string) => {
      const [a, r] = await Promise.all([
        api<Assessment>(`/devices/${id}/assessment`),
        api<Reading[]>(`/devices/${id}/readings?limit=400`),
      ]);
      // 404 on the assessment only means the hourly job has not run for this device yet.
      if (!r.ok) return fail(r.problem);
      setDetail((d) => ({ ...d, [id]: { assessment: a.ok ? a.data : null, readings: r.data, fetchedAt: Date.now() } }));
    },
    [fail],
  );

  const reload = useCallback(async () => {
    const r = await api<DeviceRow[]>("/devices");
    if (r.ok) {
      setDevices(r.data);
      await Promise.all(r.data.map((x) => loadDetail(x.device_id)));
    } else fail(r.problem);
  }, [fail, loadDetail]);

  useEffect(() => {
    void Promise.all(initial.map((x) => loadDetail(x.device_id)));
  }, [initial, loadDetail]);

  async function register(e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    const r = await api("/devices", { method: "POST", csrf, body: reg });
    if (r.ok) {
      setReg({ ...reg, device_id: "" });
      await reload();
    } else fail(r.problem);
  }

  async function assign(id: string, e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    const a = asg[id] ?? { placement: "IN_GRAIN", crop: "PADDY", interval: "300" };
    const r = await api(`/devices/${id}/assignments`, {
      method: "POST",
      csrf,
      body: { placement: a.placement, crop: a.crop, sample_interval_s: Number(a.interval) },
    });
    if (r.ok) await reload();
    else fail(r.problem);
  }

  async function deactivate(id: string) {
    if (!window.confirm(t.confirmDeactivate.replace("{id}", id))) return;
    const r = await api(`/devices/${id}/deactivate`, { method: "POST", csrf });
    if (r.ok) await reload();
    else fail(r.problem);
  }

  return (
    <>
      <Flash message={message} referenceLabel={errors.reference} />
      {devices.length === 0 && <p className="muted">{t.empty}</p>}
      {devices.map((d) => {
        const det = detail[d.device_id];
        const a = det?.assessment ?? null;
        const form = asg[d.device_id] ?? { placement: "IN_GRAIN", crop: "PADDY", interval: "300" };
        const synthetic = d.source_origin !== "DEVICE";
        return (
          <section key={d.device_id} className="card">
            <h2>
              {d.device_id}{" "}
              <span className="muted">
                · {origins[d.source_origin] ?? d.source_origin}
                {!d.active && " · ✕"}
              </span>
            </h2>
            <p className="muted">{synthetic ? t.synthetic : t.real}</p>
            <ul className="plain">
              <li>
                {t.current}:{" "}
                {d.current_assignment ? (
                  <strong>
                    {placements[d.current_assignment.placement]} · {crops[d.current_assignment.crop]}
                  </strong>
                ) : (
                  t.none
                )}
              </li>
              <li>
                {t.lastReading}: <strong>{d.last_reading_at ?? t.never}</strong>
              </li>
            </ul>
            {a ? (
              <>
                <p>
                  {t.category}:{" "}
                  <span className={`category ${a.category}`}>{categories[a.category] ?? a.category}</span>{" "}
                  <span className="muted">
                    ({t.assessedAt} {a.assessed_at})
                  </span>
                </p>
                <p className="muted">{t.label}</p>
                {a.unknown_reasons.length > 0 && (
                  <>
                    <p>{t.why}:</p>
                    <ul>
                      {a.unknown_reasons.map((r) => (
                        <li key={r}>{reasons[r] ?? r}</li>
                      ))}
                    </ul>
                  </>
                )}
                {a.rh_median_pct !== null && (
                  <ul className="plain">
                    <li>
                      {t.humidity}: <strong>{a.rh_median_pct.toFixed(1)} %</strong>
                    </li>
                    <li>
                      {t.temperature}: <strong>{a.temperature_median_c?.toFixed(1)} °C</strong>
                    </li>
                    <li>
                      {t.samples}: {a.samples_used} / {a.samples_expected}
                    </li>
                    {a.insect_flag && <li>{insects[a.insect_flag] ?? a.insect_flag}</li>}
                    {a.adjustments.map((x) => (
                      <li key={x}>{adjust[x] ?? x}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="muted">{t.noAssessment}</p>
            )}
            {det && det.readings.length === 0 && <p className="muted">{t.noReadings}</p>}
            {det && <Trend readings={det.readings} now={det.fetchedAt} label={t.trend} limits={t.limits} />}
            {canWrite && d.active && (
              <form className="form" noValidate onSubmit={(e) => void assign(d.device_id, e)}>
                <strong>{t.assign}</strong>
                <label>
                  {t.placement}
                  <select value={form.placement} onChange={(e) => setAsg({ ...asg, [d.device_id]: { ...form, placement: e.target.value } })}>
                    {Object.entries(placements).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  {t.crop}
                  <select value={form.crop} onChange={(e) => setAsg({ ...asg, [d.device_id]: { ...form, crop: e.target.value } })}>
                    {Object.entries(crops).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  {t.interval}
                  <input inputMode="numeric" value={form.interval} onChange={(e) => setAsg({ ...asg, [d.device_id]: { ...form, interval: e.target.value.replace(/\D/g, "") } })} />
                </label>
                <button type="submit" className="btn" disabled={!form.interval}>
                  {t.apply}
                </button>
              </form>
            )}
            {canManage && d.active && (
              <p>
                <button type="button" className="btn danger" onClick={() => void deactivate(d.device_id)}>
                  {t.deactivate}
                </button>
              </p>
            )}
          </section>
        );
      })}
      {canManage && (
        <>
          <h2>{t.register}</h2>
          <form onSubmit={register} className="form" noValidate>
            <label>
              {t.deviceId}
              <input required autoCapitalize="none" spellCheck={false} pattern="[a-z0-9][a-z0-9\-]{2,31}" value={reg.device_id} onChange={(e) => setReg({ ...reg, device_id: e.target.value.toLowerCase() })} />
            </label>
            <label>
              {t.origin}
              <select value={reg.source_origin} onChange={(e) => setReg({ ...reg, source_origin: e.target.value })}>
                {Object.entries(origins).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className="btn" disabled={!reg.device_id}>
              {t.submit}
            </button>
          </form>
        </>
      )}
    </>
  );
}
