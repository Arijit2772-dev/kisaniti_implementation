import { Suspense } from "react";
import { notFound } from "next/navigation";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { format, getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

/** models/v1: which forecasting model the product uses per variety and horizon, and the frozen evidence why. */
interface ModelRow {
  model: string;
  status: "PROMOTED" | "NOT_PROMOTED";
  verdict: string;
  pairs: number;
  relative_change: number;
  p_holm: number;
  ci_low: number;
  ci_high: number;
  smallest_gain: number;
}
interface Registry {
  evidence: { protocol_commit: string; results_commit: string; season: string; run_at: string; file: string; file_sha256: string };
  entries: { variety: string; horizon_days: number; in_use: string; in_use_reason: "BASELINE_ENOUGH" | "PROMOTED"; models: ModelRow[] }[];
  untested_horizons: { horizon_days: number; treatment: "SECONDARY" | "DESCRIPTIVE" }[];
}

// a change in mean absolute error against naive: negative is better; shown as a signed percentage
const pct = (x: number) => `${x > 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;

export default async function ModelsPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (_p, dict) => {
          const reg = await serverGet<Registry>("/models/registry");
          const t = dict.models;
          const names: Record<string, string> = t.names;
          const status: Record<string, string> = t.status;
          const reason: Record<string, string> = t.reason;
          const treatment: Record<string, string> = t.treatment;
          if (!reg) return <p role="alert">{t.unavailable}</p>;
          const ev = reg.evidence;
          return (
            <>
              <h1>{t.title}</h1>
              <p className="muted">{t.intro}</p>
              <section className="card">
                <h2>{t.evidenceTitle}</h2>
                <ul className="plain">
                  <li>
                    {t.season}: <strong>{ev.season}</strong> · {t.runAt}: <strong>{ev.run_at.slice(0, 10)}</strong>
                  </li>
                  <li>
                    {t.protocol}: <code>{ev.protocol_commit.slice(0, 7)}</code> · {t.results}: <code>{ev.results_commit.slice(0, 7)}</code>
                  </li>
                  <li>
                    {t.file}: <code>{ev.file}</code> (SHA-256 <code>{ev.file_sha256.slice(0, 12)}…</code>)
                  </li>
                </ul>
                <p className="muted">{t.rule}</p>
              </section>
              {reg.entries.map((e) => (
                <section key={`${e.variety}-${e.horizon_days}`} className="card" data-variety={e.variety} data-horizon={e.horizon_days}>
                  <h2>{format(t.entryTitle, { variety: e.variety, days: String(e.horizon_days) })}</h2>
                  <p>
                    <strong>
                      {t.inUse}: {names[e.in_use] ?? e.in_use}
                    </strong>{" "}
                    <span className="muted">({reason[e.in_use_reason] ?? e.in_use_reason})</span>
                  </p>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{t.model}</th>
                          <th>{t.statusLabel}</th>
                          <th>{t.change}</th>
                          <th>{t.interval}</th>
                          <th>{t.pHolm}</th>
                          <th>{t.pairs}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {e.models.map((m) => (
                          <tr key={m.model} data-status={m.status}>
                            <td data-label={t.model}>{names[m.model] ?? m.model}</td>
                            <td data-label={t.statusLabel}>{status[m.status] ?? m.status}</td>
                            <td data-label={t.change}>{pct(m.relative_change)}</td>
                            <td data-label={t.interval}>
                              {pct(m.ci_low)} … {pct(m.ci_high)}
                            </td>
                            <td data-label={t.pHolm}>{m.p_holm.toFixed(3)}</td>
                            <td data-label={t.pairs}>{m.pairs}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="muted">{format(t.gainNeeded, { gain: (e.models[0]?.smallest_gain * 100).toFixed(1) })}</p>
                </section>
              ))}
              {reg.untested_horizons.length > 0 && (
                <section className="card warn">
                  <h2>{t.untestedTitle}</h2>
                  <ul className="plain">
                    {reg.untested_horizons.map((u) => (
                      <li key={u.horizon_days}>{format(treatment[u.treatment] ?? u.treatment, { days: String(u.horizon_days) })}</li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
