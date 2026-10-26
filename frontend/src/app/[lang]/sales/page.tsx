import { Suspense } from "react";
import { notFound } from "next/navigation";
import { Protected } from "@/components/Protected";
import { SalesPanel, type PlanLine, type Sale } from "@/components/SalesPanel";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

interface PlanState {
  plan_id: string;
  status: string;
}
interface Execution {
  lines: { lot_id: string; mandi_id: string; date: string; planned_kg: string; sold_kg: string }[];
  lots: { lot_id: string; remaining_kg: string }[];
}
interface Outcomes {
  real: { pairs: number; mean_abs_error_inr_per_quintal: string | null; mean_error_inr_per_quintal: string | null };
  synthetic: { pairs: number; mean_abs_error_inr_per_quintal: string | null; mean_error_inr_per_quintal: string | null };
}

export default async function SalesPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          const [sales, lots, members, mandis, states, outcomes] = await Promise.all([
            serverGet<Sale[]>("/sales"),
            serverGet<{ id: string; member_id: string; registered_quantity_kg: string }[]>("/lots"),
            serverGet<{ id: string; display_name: string }[]>("/members"),
            serverGet<{ id: string; name: string }[]>("/mandis?limit=500"),
            serverGet<PlanState[]>("/plan-states"),
            serverGet<Outcomes>("/forecast-outcomes"),
          ]);
          const names = new Map((members ?? []).map((m) => [m.id, m.display_name]));
          const lotLabels = Object.fromEntries((lots ?? []).map((l) => [l.id, `${names.get(l.member_id) ?? l.member_id} · ${l.registered_quantity_kg} kg`]));
          const mandiName = new Map((mandis ?? []).map((m) => [m.id, m.name]));
          const approved = (states ?? []).filter((s) => s.status === "APPROVED").slice(0, 10);
          const execs = await Promise.all(approved.map((s) => serverGet<Execution>(`/plans/${s.plan_id}/execution`)));
          const planLines: PlanLine[] = approved.flatMap((s, i) =>
            (execs[i]?.lines ?? [])
              .filter((l) => Number(execs[i]?.lots.find((x) => x.lot_id === l.lot_id)?.remaining_kg ?? 0) > 0)
              .map((l) => ({
              plan_id: s.plan_id,
              lot_id: l.lot_id,
              mandi_id: l.mandi_id,
              date: l.date,
              label: `${mandiName.get(l.mandi_id) ?? l.mandi_id} · ${l.date} · ${l.planned_kg} kg`,
            })),
          );
          const t = dict.sales;
          const row = (name: string, o: Outcomes["real"]) => (
            <li key={name}>
              <strong>{name}</strong>: {t.pairs} {o.pairs}
              {o.pairs > 0 ? (
                <>
                  {" "}· {t.meanError}: <strong>{o.mean_error_inr_per_quintal}</strong> · {t.meanAbs}: <strong>{o.mean_abs_error_inr_per_quintal}</strong>
                </>
              ) : (
                <span className="muted"> · {t.noneYet}</span>
              )}
            </li>
          );
          return (
            <>
              <h1>{t.title}</h1>
              <p className="muted">{t.intro}</p>
              <SalesPanel
                lang={lang}
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                canVoid={p.capabilities.includes("plans.approve")}
                t={t}
                errors={dict.errors}
                initial={sales ?? []}
                lotLabels={lotLabels}
                mandis={mandis ?? []}
                planLines={planLines}
              />
              {outcomes && (
                <>
                  <h2>{t.outcomesTitle}</h2>
                  <p className="muted">{t.outcomesNote}</p>
                  <ul className="plain">
                    {row(t.real, outcomes.real)}
                    {row(t.synthetic, outcomes.synthetic)}
                  </ul>
                </>
              )}
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
