import { Suspense } from "react";
import { notFound } from "next/navigation";
import { LotsPanel, type CommodityRef, type DeviceRef, type LinkRow, type LocationRow, type LotRow, type MemberRef } from "@/components/LotsPanel";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

export default async function LotsPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          const [lots, locations, members, commodities, devices, links] = await Promise.all([
            serverGet<LotRow[]>("/lots"),
            serverGet<LocationRow[]>("/locations"),
            serverGet<MemberRef[]>("/members"),
            serverGet<CommodityRef[]>("/commodities"),
            serverGet<DeviceRef[]>("/devices"),
            serverGet<LinkRow[]>("/sensor-links/current"),
          ]);
          return (
            <>
              <h1>{dict.lots.title}</h1>
              <p className="muted">{dict.lots.intro}</p>
              <LotsPanel
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                t={dict.lots}
                errors={dict.errors}
                initialLots={lots ?? []}
                initialLocations={locations ?? []}
                members={members ?? []}
                commodities={commodities ?? []}
                devices={devices ?? []}
                initialLinks={links ?? []}
              />
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
