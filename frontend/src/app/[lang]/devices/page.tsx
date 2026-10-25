import { Suspense } from "react";
import { notFound } from "next/navigation";
import { DevicesPanel, type DeviceRow } from "@/components/DevicesPanel";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

export default async function DevicesPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          const initial = (await serverGet<DeviceRow[]>("/devices")) ?? [];
          return (
            <>
              <h1>{dict.devices.title}</h1>
              <p className="muted">{dict.devices.intro}</p>
              <DevicesPanel
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                canManage={p.capabilities.includes("users.manage")}
                t={dict.devices}
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
