import { Suspense } from "react";
import { notFound } from "next/navigation";
import { MembersPanel, type MemberRow } from "@/components/MembersPanel";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

export default async function MembersPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          const initial = (await serverGet<MemberRow[]>("/members")) ?? [];
          return (
            <>
              <h1>{dict.members.title}</h1>
              <p className="muted">{dict.members.intro}</p>
              <MembersPanel
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                t={dict.members}
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
