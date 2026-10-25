import { Suspense } from "react";
import { notFound } from "next/navigation";
import { Protected } from "@/components/Protected";
import { UsersPanel, type UserRow } from "@/components/UsersPanel";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

export default async function UsersPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (p, dict) => {
          if (!p.capabilities.includes("users.manage")) return <p className="issue">{dict.users.noPermission}</p>;
          const initial = (await serverGet<UserRow[]>("/users")) ?? [];
          return (
            <>
              <h1>{dict.users.title}</h1>
              <p className="muted">{dict.users.intro}</p>
              <UsersPanel csrf={p.csrf_token} selfId={p.user_id} t={dict.users} errors={dict.errors} roles={dict.shell.roles} initialUsers={initial} />
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
