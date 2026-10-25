import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { Shell } from "@/components/Shell";
import type { Locale } from "@/i18n/config";
import { getDictionary, type Dictionary } from "@/i18n/dictionaries";
import { getPrincipal, type Principal } from "@/lib/session";

/** Pages that need a signed-in user. Not signed in: to the sign-in page. A pending password change: to that page. */
export async function Protected({
  lang,
  allowPendingPassword = false,
  children,
}: {
  lang: Locale;
  allowPendingPassword?: boolean;
  children: (p: Principal, d: Dictionary) => ReactNode | Promise<ReactNode>;
}) {
  const p = await getPrincipal();
  if (!p) redirect(`/${lang}/sign-in`);
  if (p.must_change_password && !allowPendingPassword) redirect(`/${lang}/change-password`);
  const d = await getDictionary(lang);
  return (
    <Shell lang={lang} p={p} d={d} limited={p.must_change_password}>
      {await children(p, d)}
    </Shell>
  );
}
