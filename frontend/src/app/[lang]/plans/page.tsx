import { Suspense } from "react";
import { notFound } from "next/navigation";
import { PlansPanel, type PlanSummary } from "@/components/PlansPanel";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

export default async function PlansPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          const initial = (await serverGet<PlanSummary[]>("/plans")) ?? [];
          return (
            <>
              <h1>{dict.plans.title}</h1>
              <p className="muted">{dict.plans.intro}</p>
              <PlansPanel
                lang={lang}
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                t={dict.plans}
                datasetStatus={dict.prices.status}
                errors={dict.errors}
                initial={initial}
              />
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
