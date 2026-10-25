import { Suspense } from "react";
import { notFound } from "next/navigation";
import { LanguageSwitch } from "@/components/LanguageSwitch";
import { StatusCard } from "@/components/StatusCard";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";

// Public: the health of the system does not need a login (no personal data on this page).
export default async function StatusPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <main>
      <LanguageSwitch lang={lang} label={d.lang.label} path="/status" />
      <h1>{d.app.name}</h1>
      <p className="muted">{d.app.tagline}</p>
      <Suspense fallback={<p className="muted">{d.status.checking}</p>}>
        <StatusCard d={d} />
      </Suspense>
    </main>
  );
}
