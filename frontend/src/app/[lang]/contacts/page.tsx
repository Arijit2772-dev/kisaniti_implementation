import { Suspense } from "react";
import { notFound } from "next/navigation";
import { ContactsPanel, type ContactRow } from "@/components/ContactsPanel";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

export default async function ContactsPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          const rows = (await serverGet<ContactRow[]>("/contacts")) ?? [];
          return (
            <>
              <h1>{dict.contacts.title}</h1>
              <p className="muted">{dict.contacts.intro}</p>
              <ContactsPanel
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                t={dict.contacts}
                langs={dict.members.langs}
                errors={dict.errors}
                initial={rows}
              />
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
