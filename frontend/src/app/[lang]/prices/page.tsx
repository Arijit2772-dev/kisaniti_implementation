import { Suspense } from "react";
import { notFound } from "next/navigation";
import { PriceBrowser, type MandiRef } from "@/components/PriceBrowser";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { format, getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

interface Summary {
  dataset: { name: string; rows: number; dated_rows: number; blank_date_rows: number; flagged_rows: number; validation_status: string } | null;
  newest_observed_on: string | null;
  days_since_newest: number | null;
  by_season_variety: { season: string; variety_label: string; rows: number; first_on: string; last_on: string }[];
}
interface Msp {
  crop: string;
  grade: string;
  marketing_season: string;
  msp_inr_per_quintal: string;
  source_url: string;
  verified: boolean;
}

export default async function PricesPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (_p, dict) => {
          const [summary, msp, mandis] = await Promise.all([
            serverGet<Summary>("/prices/summary"),
            serverGet<Msp[]>("/msp"),
            serverGet<MandiRef[]>("/mandis?limit=500"),
          ]);
          const t = dict.prices;
          const status: Record<string, string> = t.status;
          const grades: Record<string, string> = t.grades;
          const crops: Record<string, string> = t.crops;
          const ds = summary?.dataset ?? null;
          const varieties = [...new Set((summary?.by_season_variety ?? []).map((g) => g.variety_label))];
          return (
            <>
              <h1>{t.title}</h1>
              <p className="muted">{t.intro}</p>
              <h2>{t.datasetTitle}</h2>
              {ds === null ? (
                <p className="muted">{t.noData}</p>
              ) : (
                <section className={ds.validation_status === "VALIDATED" ? "card ready" : "card warn"}>
                  <p>
                    <strong>
                      {t.statusLabel}: {status[ds.validation_status] ?? ds.validation_status}
                    </strong>{" "}
                    <span className="muted">({ds.name})</span>
                  </p>
                  {ds.validation_status !== "VALIDATED" && <p role="note">{t.pendingWarning}</p>}
                  <ul className="plain">
                    <li>
                      {t.rows}: <strong>{ds.rows}</strong>
                    </li>
                    <li>
                      {t.dated}: <strong>{ds.dated_rows}</strong>
                    </li>
                    <li>
                      {t.blankDate}: <strong>{ds.blank_date_rows}</strong>
                    </li>
                    <li>
                      {t.flagged}: <strong>{ds.flagged_rows}</strong>
                    </li>
                    {summary?.newest_observed_on && (
                      <li>
                        {t.newest}: <strong>{summary.newest_observed_on}</strong>{" "}
                        <span className="muted">
                          ({format(t.daysAgo, { days: String(summary.days_since_newest ?? 0) })})
                        </span>
                      </li>
                    )}
                  </ul>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{t.season}</th>
                          <th>{t.variety}</th>
                          <th>{t.count}</th>
                          <th>{t.first}</th>
                          <th>{t.last}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {summary?.by_season_variety.map((g) => (
                          <tr key={`${g.season}-${g.variety_label}`}>
                            <td data-label={t.season}>{g.season}</td>
                            <td data-label={t.variety}>{g.variety_label}</td>
                            <td data-label={t.count}>{g.rows}</td>
                            <td data-label={t.first}>{g.first_on}</td>
                            <td data-label={t.last}>{g.last_on}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
              <PriceBrowser t={t} errors={dict.errors} mandis={mandis ?? []} varieties={varieties} />
              <h2>{t.mspTitle}</h2>
              <p className="muted">{t.perQuintal}</p>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t.mspSeason}</th>
                      <th>{t.crop}</th>
                      <th>{t.mspGrade}</th>
                      <th>MSP</th>
                      <th>{t.source}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(msp ?? []).map((m) => (
                      <tr key={`${m.crop}-${m.marketing_season}-${m.grade}`}>
                        <td data-label={t.mspSeason}>{m.marketing_season}</td>
                        <td data-label={t.crop}>{crops[m.crop] ?? m.crop}</td>
                        <td data-label={t.mspGrade}>{grades[m.grade] ?? m.grade}</td>
                        <td data-label="MSP">
                          <strong>{m.msp_inr_per_quintal}</strong>
                          {!m.verified && <span className="muted"> ?</span>}
                        </td>
                        <td data-label={t.source}>
                          <a href={m.source_url} rel="noopener noreferrer" target="_blank">
                            {t.source}
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
